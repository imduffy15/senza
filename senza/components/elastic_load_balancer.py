import click
from clickclick import fatal_error
from collections import defaultdict

from senza.aws import resolve_security_groups
from ..cli import AccountArguments, TemplateArguments
from ..exceptions import InvalidState
from ..manaus import ClientError
from ..manaus.iam import IAM, IAMServerCertificate
from ..manaus.acm import ACM, ACMCertificate
from ..manaus.route53 import Route53

SENZA_PROPERTIES = frozenset(['Domains', 'HealthCheckPath', 'HealthCheckPort', 'HealthCheckProtocol',
                              'HTTPPort', 'Name', 'SecurityGroups', 'SSLCertificateId', 'Type'])


def get_load_balancer_name(stack_name: str, stack_version: str):
    '''
    >>> get_load_balancer_name('a', '1')
    'a-1'

    >>> get_load_balancer_name('toolong123456789012345678901234567890', '1')
    'toolong12345678901234567890123-1'
    '''
    # Loadbalancer name cannot exceed 32 characters, try to shorten
    l = 32 - len(stack_version) - 1
    return '{}-{}'.format(stack_name[:l], stack_version)


def get_listeners(subdomain, main_zone, configuration,
                  account_info: AccountArguments):
    ssl_cert = configuration.get('SSLCertificateId')

    if ACMCertificate.arn_is_acm_certificate(ssl_cert):
        # check if certificate really exists
        try:
            ACMCertificate.get_by_arn(account_info.Region, ssl_cert)
        except ClientError as e:
            error_msg = e.response['Error']['Message']
            fatal_error(error_msg)
    elif IAMServerCertificate.arn_is_server_certificate(ssl_cert):
        # TODO check if certificate exists
        pass
    elif ssl_cert is not None:
        certificate = IAMServerCertificate.get_by_name(ssl_cert)
        ssl_cert = certificate.arn
    elif main_zone is not None:
        if main_zone:
            iam_pattern = main_zone.lower().rstrip('.').replace('.', '-')
            name = '{sub}.{zone}'.format(sub=subdomain,
                                         zone=main_zone.rstrip('.'))
            acm = ACM(account_info.Region)
            acm_certificates = sorted(acm.get_certificates(domain_name=name),
                                      reverse=True)
        else:
            iam_pattern = ''
            acm_certificates = []

        iam_certificates = sorted(IAM.get_certificates(name=iam_pattern))
        if not iam_certificates:
            # if there are no iam certificates matching the pattern
            # try to use any certificate
            iam_certificates = sorted(IAM.get_certificates(), reverse=True)

        # the priority is acm_certificate first and iam_certificate second
        certificates = (acm_certificates +
                        iam_certificates)  # type: List[Union[ACMCertificate, IAMServerCertificate]]
        try:
            certificate = certificates[0]
            ssl_cert = certificate.arn
        except IndexError:
            if main_zone:
                fatal_error('Could not find any matching '
                            'SSL certificate for "{}"'.format(name))
            else:
                fatal_error('Could not find any SSL certificate')
    return [
        {
            "PolicyNames": [],
            "SSLCertificateId": ssl_cert,
            "Protocol": "HTTPS",
            "InstancePort": configuration["HTTPPort"],
            "LoadBalancerPort": 443
        }
    ]


def component_elastic_load_balancer(definition,
                                    configuration: dict,
                                    args: TemplateArguments,
                                    info: dict,
                                    force,
                                    account_info: AccountArguments):
    lb_name = configuration["Name"]
    # domains pointing to the load balancer
    subdomain = ''
    main_zone = None
    for name, domain in configuration.get('Domains', {}).items():
        name = '{}{}'.format(lb_name, name)

        domain_name = "{0}.{1}".format(domain["Subdomain"], domain["Zone"])
        records = Route53.get_records(name=domain_name)
        converted_records = defaultdict(lambda: {'to_delete': [],
                                                 'to_upsert': []})
        for record in records:
            if record.type != 'A':
                converted_records[record.hosted_zone]['to_delete'].append(record)
                converted_records[record.hosted_zone]['to_upsert'].append(record.to_alias())

        if converted_records:
                for hosted_zone, records in converted_records.items():
                    if click.confirm("\n  {name} ({hz}): {n} records need "
                                     "to be converted to "
                                     "Alias records".format(name=domain_name,
                                                            hz=hosted_zone.name,
                                                            n=len(records['to_upsert']))):
                        hosted_zone.delete(records['to_delete'],
                                           comment="Records that will be "
                                                   "converted to Alias")
                        # TODO fix the delete

                        hosted_zone.upsert(records['to_upsert'],
                                           comment="Converted non alias records")
                    else:
                        raise InvalidState("Can't create domains because there are "
                                           "non A Type records.")

        properties = {"Type": "A",
                      "Name": domain_name,
                      "HostedZoneName": "{0}".format(domain["Zone"]),
                      "AliasTarget": {"HostedZoneId": {"Fn::GetAtt": [lb_name,
                                                                      "CanonicalHostedZoneNameID"]},
                                      "DNSName": {"Fn::GetAtt": [lb_name, "DNSName"]}}}
        definition["Resources"][name] = {"Type": "AWS::Route53::RecordSet",
                                         "Properties": properties}

        if domain["Type"] == "weighted":
            definition["Resources"][name]["Properties"]['Weight'] = 0
            definition["Resources"][name]["Properties"]['SetIdentifier'] = "{0}-{1}".format(info["StackName"],
                                                                                            info["StackVersion"])
            subdomain = domain['Subdomain']
            main_zone = domain['Zone']  # type: str

    listeners = configuration.get('Listeners') or get_listeners(subdomain, main_zone, configuration, account_info)

    health_check_protocol = "HTTP"
    allowed_health_check_protocols = ("HTTP", "TCP", "UDP", "SSL")
    if "HealthCheckProtocol" in configuration:
        health_check_protocol = configuration["HealthCheckProtocol"]

    if health_check_protocol not in allowed_health_check_protocols:
        raise click.UsageError('Protocol "{}" is not supported for LoadBalancer'.format(health_check_protocol))

    health_check_path = "/ui/"
    if "HealthCheckPath" in configuration:
        health_check_path = configuration["HealthCheckPath"]

    health_check_port = configuration["HTTPPort"]
    if "HealthCheckPort" in configuration:
        health_check_port = configuration["HealthCheckPort"]

    health_check_target = "{0}:{1}{2}".format(health_check_protocol,
                                              health_check_port,
                                              health_check_path)

    if configuration.get('NameSuffix'):
        version = '{}-{}'.format(info["StackVersion"],
                                 configuration['NameSuffix'])
        loadbalancer_name = get_load_balancer_name(info["StackName"], version)
        del(configuration['NameSuffix'])
    else:
        loadbalancer_name = get_load_balancer_name(info["StackName"],
                                                   info["StackVersion"])

    loadbalancer_scheme = "internal"
    allowed_loadbalancer_schemes = ("internet-facing", "internal")
    if "Scheme" in configuration:
        loadbalancer_scheme = configuration["Scheme"]
    else:
        configuration["Scheme"] = loadbalancer_scheme

    if loadbalancer_scheme == 'internet-facing':
        click.secho('You are deploying an internet-facing ELB that will be '
                    'publicly accessible! You should have OAUTH2 and HTTPS '
                    'in place!', bold=True, err=True)

    if loadbalancer_scheme not in allowed_loadbalancer_schemes:
        raise click.UsageError('Scheme "{}" is not supported for LoadBalancer'.format(loadbalancer_scheme))

    if loadbalancer_scheme == "internal":
        loadbalancer_subnet_map = "LoadBalancerInternalSubnets"
    else:
        loadbalancer_subnet_map = "LoadBalancerSubnets"

    # load balancer
    definition["Resources"][lb_name] = {
        "Type": "AWS::ElasticLoadBalancing::LoadBalancer",
        "Properties": {
            "Subnets": {"Fn::FindInMap": [loadbalancer_subnet_map, {"Ref": "AWS::Region"}, "Subnets"]},
            "HealthCheck": {
                "HealthyThreshold": "2",
                "UnhealthyThreshold": "2",
                "Interval": "10",
                "Timeout": "5",
                "Target": health_check_target
            },
            "Listeners": listeners,
            "ConnectionDrainingPolicy": {
                "Enabled": True,
                "Timeout": 60
            },
            "CrossZone": "true",
            "LoadBalancerName": loadbalancer_name,
            "SecurityGroups": resolve_security_groups(configuration["SecurityGroups"], args.region),
            "Tags": [
                # Tag "Name"
                {
                    "Key": "Name",
                    "Value": "{0}-{1}".format(info["StackName"], info["StackVersion"])
                },
                # Tag "StackName"
                {
                    "Key": "StackName",
                    "Value": info["StackName"],
                },
                # Tag "StackVersion"
                {
                    "Key": "StackVersion",
                    "Value": info["StackVersion"]
                }
            ]
        }
    }
    for key, val in configuration.items():
        # overwrite any specified properties, but
        # ignore our special Senza properties as they are not supported by CF
        if key not in SENZA_PROPERTIES:
            definition['Resources'][lb_name]['Properties'][key] = val

    return definition
