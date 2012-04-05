import settings

import fabric
import fabric.api
from fabric.operations import sudo, run, put, get
from fabric.exceptions import NetworkError
import StringIO

class ChromaManagerOps(object):
    def __init__(self, manager):
        self.manager = manager
        self.ec2_session = manager.get_session()

    def setup_chroma_repo(self):
        sudo('mkdir -p /root/keys')
        for key in ['chroma_ca-cacert.pem', 'privkey-nopass.pem', 'test-ami-cert.pem']:
            put("%s/%s" % (settings.YUM_KEYS, key), "/root/keys/%s" % key, use_sudo = True)
        put(settings.YUM_REPO, "/etc/yum.repos.d", use_sudo = True)

    def install_deps(self):
        with self.ec2_session.fabric_settings():
            run('wget http://download.fedoraproject.org/pub/epel/6/i386/epel-release-6-5.noarch.rpm')
            sudo('rpm -i --force epel-release-6-5.noarch.rpm')
            self.setup_chroma_repo()
            sudo('yum install -y hydra-server')

    # Temporary hack until image creation is refactored
    def install_app_deps(self):
        with self.ec2_session.fabric_settings():
            self.setup_chroma_repo()
            sudo('yum install -y lustre')
            sudo('grubby --set-default "/boot/vmlinuz-2.6.32-*lustre*"')
            sudo('yum install -y hydra-agent-management')

    def setup_chroma(self):
        with self.ec2_session.fabric_settings():
            sudo("echo \"%s %s\" >> /etc/hosts" % (self.ec2_session.instance.ip_address, 
                                                   self.manager.node.name))
            sudo("chroma-config setup %s %s" % (
                settings.CHROMA_MANAGER_USER,
                settings.CHROMA_MANAGER_PASSWORD))

    def reset_chroma(self):
        with self.ec2_session.fabric_settings():
            sudo("chroma-config start")

    def create_keys(self):
        with self.ec2_session.fabric_settings():
            with fabric.api.settings(warn_only = True):
                result = get(".ssh/id_rsa.pub", open("/dev/null", 'w'))
            if result.failed:
                sudo('ssh-keygen -t rsa -N "" -f .ssh/id_rsa')

    def get_key(self):
        buf = StringIO.StringIO()
        with self.ec2_session.fabric_settings():
            get(".ssh/id_rsa.pub", buf)
        return buf.getvalue()

    def add_server(self, appliance_ops):
        with self.ec2_session.fabric_settings():
            sudo("echo \"%s %s\" >> /etc/hosts" % (appliance_ops.ec2_session.instance.ip_address, 
                                                   appliance_ops.appliance.node.name))

#        from provisioning.lib.chroma_manager_client import AuthorizedHttpRequests
#        manager_url = "http://%s/" % self.ec2_session.instance.ip_address
#        appliance_address = "%s@%s" % (settings.CHROMA_APPLIANCE['username'],
#                appliance_ops.ec2_session.instance.ip_address)
#        requests = AuthorizedHttpRequests(settings.CHROMA_MANAGER_USER, settings.CHROMA_MANAGER_PASSWORD, manager_url)
#        response = requests.post("/api/host/", body = {'address': appliance_address})
#        assert(response.successful)

class ChromaApplianceOps(object):
    def __init__(self, appliance):
        self.appliance = appliance
        self.ec2_session = appliance.get_session()

    def set_key(self, key):
        with self.ec2_session.fabric_settings():
            sudo("echo \"%s\" >> .ssh/authorized_keys" % key)
            sudo("echo \"%s %s\" >> /etc/hosts" % (self.ec2_session.instance.ip_address, 
                                               self.appliance.node.name))
            sudo("hostname %s" %(self.appliance.node.name))
