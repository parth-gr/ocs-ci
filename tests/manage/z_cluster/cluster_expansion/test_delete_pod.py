import pytest

import logging
from concurrent.futures import ThreadPoolExecutor

from ocs_ci.framework.testlib import ignore_leftovers, tier4c, skipif_managed_service
from ocs_ci.framework.pytest_customization.marks import (
    skipif_external_mode,
    brown_squad,
)
from ocs_ci.framework import config
from ocs_ci.ocs import constants
from ocs_ci.ocs.ocp import OCP
from ocs_ci.ocs.resources import pod as pod_helpers
from ocs_ci.ocs.resources import storage_cluster
from ocs_ci.ocs.cluster import (
    get_percent_used_capacity,
    check_ceph_health_after_add_capacity,
    is_flexible_scaling_enabled,
)
from ocs_ci.helpers.disruption_helpers import Disruptions
from ocs_ci.ocs import node

logger = logging.getLogger(__name__)


@brown_squad
@skipif_managed_service
@skipif_external_mode
@ignore_leftovers
@tier4c
class TestAddCapacityWithResourceDelete:
    """
    Test add capacity when one of the resources gets deleted
    in the middle of the process.
    """

    new_pods_in_status_running = False

    @pytest.fixture(autouse=True)
    def setup(self, add_nodes):
        """
        Check that we have the right configurations before we start the test
        """
        osd_pods_before = pod_helpers.get_osd_pods()
        number_of_osd_pods_before = len(osd_pods_before)
        if number_of_osd_pods_before >= constants.MAX_OSDS:
            pytest.skip("We have maximum of OSDs in the cluster")

        # If we use vSphere we may need to add more worker nodes
        # to the cluster before starting the test
        if (
            config.ENV_DATA["platform"].lower() == constants.VSPHERE_PLATFORM
            and number_of_osd_pods_before >= 9
        ):
            num_of_expected_wnodes = 6
            wnodes = node.get_worker_nodes()
            num_of_wnodes = len(wnodes)
            logger.info(
                f"We have {number_of_osd_pods_before} OSDs in the cluster, "
                f"and {num_of_wnodes} worker nodes in the cluster"
            )
            if num_of_wnodes < num_of_expected_wnodes:
                num_of_wnodes_to_add = num_of_expected_wnodes - num_of_wnodes
                logger.info(
                    f"Adding more {num_of_wnodes_to_add} worker nodes to the cluster"
                )
                add_nodes(ocs_nodes=False, node_count=num_of_wnodes_to_add)

            wnodes_not_in_ocs = node.get_worker_nodes_not_in_ocs()
            if wnodes_not_in_ocs:
                logger.info("Label the worker nodes that are not in OCS")
                node.label_nodes(wnodes_not_in_ocs)

    def kill_resource_repeatedly(self, resource_name, resource_id, max_iterations=30):
        """
        The function get the resource name, and id and kill the resource repeatedly
        until the new osd pods reached status running.

        Args:
            resource_name (str): the name of the resource to kill
            resource_id (int): the id of the resource to kill
            max_iterations (int): Maximum times of iterations to delete the given resource

        """
        d = Disruptions()

        for i in range(max_iterations):
            logger.info(
                f"iteration {i}: Delete resource {resource_name} with id {resource_id}"
            )
            d.set_resource(resource_name)
            d.delete_resource(resource_id)
            if self.new_pods_in_status_running:
                logger.info("New osd pods reached status running")
                break

        if not self.new_pods_in_status_running:
            logger.warning(
                f"New osd pods didn't reach status running after {max_iterations} iterations"
            )

    def wait_for_osd_pods_to_be_running(self, storagedeviceset_count):
        """
        The function gets the number of storage device set in the cluster, and wait
        for the osd pods to be in status running.

        Args:
            storagedeviceset_count (int): the number of storage device set in the cluster

        """
        logger.info("starting function 'wait_for_osd_pods_to_be_running'")
        pod = OCP(kind=constants.POD, namespace=config.ENV_DATA["cluster_namespace"])
        if is_flexible_scaling_enabled():
            replica_count = 1
        else:
            replica_count = 3
        pod.wait_for_resource(
            timeout=420,
            condition=constants.STATUS_RUNNING,
            selector="app=rook-ceph-osd",
            resource_count=storagedeviceset_count * replica_count,
        )
        self.new_pods_in_status_running = True

    @pytest.mark.parametrize(
        argnames="workload_storageutilization_rbd, resource_name, resource_id, is_kill_resource_repeatedly",
        argvalues=[
            pytest.param(
                *[(0.11, True, 120), constants.OSD, 0, False],
                marks=pytest.mark.polarion_id("OCS-1203"),
            ),
            pytest.param(
                *[(0.11, True, 120), constants.ROOK_OPERATOR, 0, False],
                marks=pytest.mark.polarion_id("OCS-1206"),
            ),
            pytest.param(
                *[(0.11, True, 120), constants.MON_DAEMON, 0, True],
                marks=pytest.mark.polarion_id("OCS-1207"),
            ),
        ],
        indirect=["workload_storageutilization_rbd"],
    )
    def test_add_capacity_with_resource_delete(
        self,
        add_capacity_setup,
        workload_storageutilization_rbd,
        resource_name,
        resource_id,
        is_kill_resource_repeatedly,
    ):
        """
        The function get the resource name, and id.
        The function adds capacity to the cluster, and then delete the resource while
        storage capacity is getting increased.

        Args:
            resource_name (str): the name of the resource to delete
            resource_id (int): the id of the resource to delete
            is_kill_resource_repeatedly (bool): If True then kill the resource repeatedly. Else, if False
                delete the resource only once.

        """
        used_percentage = get_percent_used_capacity()
        logger.info(
            f"storageutilization is completed. used capacity = {used_percentage}"
        )

        osd_pods_before = pod_helpers.get_osd_pods()
        number_of_osd_pods_before = len(osd_pods_before)

        d = Disruptions()
        d.set_resource(resource_name)

        self.new_pods_in_status_running = False

        osd_size = storage_cluster.get_osd_size()
        logger.info(f"Adding one new set of OSDs. osd size = {osd_size}")
        storagedeviceset_count = storage_cluster.add_capacity(osd_size)
        logger.info("Adding one new set of OSDs was issued without problems")

        # Wait for new osd's to come up. After the first new osd in status Init - delete the resource.
        # After deleting the resource we expect that all the new osd's will be in status running,
        # and the delete resource will be also in status running.
        pod_helpers.wait_for_new_osd_pods_to_come_up(number_of_osd_pods_before)
        logger.info(
            f"Delete a {resource_name} pod while storage capacity is getting increased"
        )
        if is_kill_resource_repeatedly:
            with ThreadPoolExecutor() as executor:
                executor.submit(
                    self.kill_resource_repeatedly, resource_name, resource_id
                )
                self.wait_for_osd_pods_to_be_running(storagedeviceset_count)
        else:
            d.delete_resource(resource_id)
            self.wait_for_osd_pods_to_be_running(storagedeviceset_count)

        self.new_pods_in_status_running = True
        logger.info("Finished verifying add capacity when one of the pods gets deleted")
        logger.info("Waiting for ceph health check to finished...")
        check_ceph_health_after_add_capacity()
