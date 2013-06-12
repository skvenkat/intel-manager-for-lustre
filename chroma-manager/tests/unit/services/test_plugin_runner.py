from chroma_core.models import VolumeNode
import mock
from django.test import TestCase
from chroma_core.services.plugin_runner import AgentPluginHandlerCollection
from tests.unit.chroma_core.helper import synthetic_host, load_default_profile, synthetic_volume_full


class TestRebalancePassthrough(TestCase):
    """
    Validate that the rebalance_host_volumes member function correctly calls through
    to resource manager
    """

    def setUp(self):
        load_default_profile()

        # Initialise storage plugin stuff for the benefit of synthetic_volume_full
        import chroma_core.lib.storage_plugin.manager
        chroma_core.lib.storage_plugin.manager.storage_plugin_manager = chroma_core.lib.storage_plugin.manager.StoragePluginManager()

    def test_multiple_volume_nodes(self):
        """
        Test that when a volume has multiple volume nodes on one host, the volume is
        not duplicated in the arguments to resource manager (HYD-2119)
        """
        host = synthetic_host()
        volume = synthetic_volume_full(host)

        # An extra volume node, so that there are now two on one host
        VolumeNode.objects.create(volume=volume, host=host, path="/dev/sdaxxx")
        self.assertEqual(VolumeNode.objects.filter(host=host).count(), 2)

        resource_manager = mock.Mock()
        AgentPluginHandlerCollection(resource_manager).rebalance_host_volumes(host.id)
        called_with_volumes = list(resource_manager.balance_unweighted_volume_nodes.call_args[0][0])
        self.assertListEqual(called_with_volumes, [volume])
