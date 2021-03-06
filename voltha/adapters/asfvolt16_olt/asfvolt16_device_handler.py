#
# Copyright 2017 the original author or authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

"""
Asfvolt16 OLT adapter
"""


from scapy.layers.l2 import Ether, Dot1Q
from uuid import uuid4
from common.frameio.frameio import BpfProgramFilter
from twisted.internet import reactor
from common.frameio.frameio import hexify
from scapy.packet import Packet
import voltha.core.flow_decomposer as fd
from voltha.protos.common_pb2 import OperStatus, ConnectStatus
from voltha.protos.device_pb2 import Port
from voltha.protos.common_pb2 import AdminState
from voltha.protos.logical_device_pb2 import LogicalPort, LogicalDevice
from voltha.protos.openflow_13_pb2 import OFPPS_LIVE, OFPPF_FIBER, \
    OFPPF_1GB_FD, OFPC_GROUP_STATS, OFPC_PORT_STATS, OFPC_TABLE_STATS, \
    OFPC_FLOW_STATS, ofp_switch_features, ofp_desc, ofp_port, \
    OFPXMC_OPENFLOW_BASIC
from voltha.core.logical_device_agent import mac_str_to_tuple
from voltha.adapters.asfvolt16_olt.bal import Bal
from voltha.adapters.device_handler import OltDeviceHandler
from voltha.protos.bbf_fiber_base_pb2 import \
    ChannelgroupConfig, ChannelpartitionConfig, ChannelpairConfig, \
    ChannelterminationConfig, OntaniConfig, VOntaniConfig, VEnetConfig
from voltha.protos.bbf_fiber_traffic_descriptor_profile_body_pb2 import \
    TrafficDescriptorProfileData
from voltha.protos.bbf_fiber_tcont_body_pb2 import TcontsConfigData
from voltha.protos.bbf_fiber_gemport_body_pb2 import GemportsConfigData
from voltha.protos.bbf_fiber_multicast_gemport_body_pb2 import \
    MulticastGemportsConfigData
from voltha.protos.bbf_fiber_multicast_distribution_set_body_pb2 import \
    MulticastDistributionSetData
import time

ASFVOLT_NNI_PORT = 50
# ASFVOLT_NNI_PORT needs to be other than pon port value.
# Edgecore OLT assigns PONport between 0 to 15, hence
# having a value 50 for NNI port to avoid collision.
# TODO: VLAN ID needs to come from some sort of configuration.
PACKET_IN_VLAN = 4091
is_inband_frame = BpfProgramFilter('(ether[14:2] & 0xfff) = 0x{:03x}'.format(
    PACKET_IN_VLAN))

ASFVOLT_EAPOL_ID = 1
ASFVOLT_DHCP_UNTAGGED_ID = 2
ASFVOLT_DHCP_TAGGED_ID = 3
ASFVOLT_IGMP_UNTAGGED_ID = 4
ASFVOLT_IGMP_TAGGED_ID = 5
ASFVOLT_FIRMWARE_ID = 6
ASFVOLT_ARP_ID = 7
ASFVOLT_HSIA_ID = 8
ASFVOLT_DNS_ID = 9

ASFVOLT_DOWNLINK_EAPOL_ID = 10
ASFVOLT_DOWNLINK_DHCP_UNTAGGED_ID = 11
ASFVOLT_DOWNLINK_DHCP_TAGGED_ID = 12
ASFVOLT_DOWNLINK_IGMP_UNTAGGED_ID = 13
ASFVOLT_DOWNLINK_IGMP_TAGGED_ID = 14
ASFVOLT_DOWNLINK_FIRMWARE_ID = 15
ASFVOLT_DOWNLINK_ARP_ID = 16
ASFVOLT_DOWNLINK_HSIA_ID = 17
ASFVOLT_DOWNLINK_DNS_ID = 18


class VEnetHandler(object):

    def __init__(self):
        self.v_enet = VEnetConfig()
        self.gem_ports = dict()


class VOntAniHandler(object):

    def __init__(self):
        self.v_ont_ani = VOntaniConfig()
        self.tconts = dict()


class Asfvolt16Handler(OltDeviceHandler):

    def __init__(self, adapter, device_id):
        super(Asfvolt16Handler, self).__init__(adapter, device_id)
        self.filter = is_inband_frame
        self.bal = Bal(self, self.log)
        self.host_and_port = None
        self.olt_id = 0
        self.channel_groups = dict()
        self.channel_partitions = dict()
        self.channel_pairs = dict()
        self.channel_terminations = dict()
        self.v_ont_anis = dict()
        self.ont_anis = dict()
        self.v_enets = dict()
        self.traffic_descriptors = dict()
        self.uni_port_num = 20

    def __del__(self):
        super(Asfvolt16Handler, self).__del__()

    def __str__(self):
        return "Asfvolt16Handler: {}".format(self.host_and_port)

    def _get_next_uni_port(self):
        self.uni_port_num += 1
        return self.uni_port_num

    def get_venet(self, name):
        for key, v_enet in self.v_enets.items():
            if key == name:
                return v_enet
        return None

    def get_v_ont_ani(self, name):
        for key, v_ont_ani in self.v_ont_anis.items():
            if key == name:
                return v_ont_ani
        return None

    def get_gem_port_info(self, v_enet, **kwargs):
        traffic_class = kwargs.pop('traffic_class', None)
        name = kwargs.pop('name', None)
        for key, gem_port in v_enet.gem_ports.items():
            if traffic_class is not None:
                if traffic_class == gem_port.traffic_class:
                    return gem_port
            if name is not None:
                if name == gem_port.name:
                    return gem_port
        return None

    def get_tcont_info(self, v_ont_ani, **kwargs):
        alloc_id = kwargs.pop('alloc_id', None)
        name = kwargs.pop('name', None)
        for key, tcont in v_ont_ani.tconts.items():
            if alloc_id is not None:
                if alloc_id == tcont.alloc_id:
                    return tcont
            if name is not None:
                if name == tcont.name:
                    return tcont
        return None

    def get_traffic_profile(self, name):
        for key, traffic_profile in self.traffic_descriptors():
            if name is not None:
                if name == traffic_profile.name:
                    return traffic_profile
        return None

    def get_flow_id(self, onu_id, intf_id, id):
        # Tp-Do Need to generate unique flow ID using
        # OnuID, IntfId, id
        # BAL accepts flow_id till 16384. So we are
        # using only onu_id and id to generate flow ID.
        return ((onu_id << 13) | id)

    def activate(self, device):

        self.log.info('activating-asfvolt16-olt', device=device)

        if self.logical_device_id is None:

            if not device.host_and_port:
                device.oper_status = OperStatus.FAILED
                device.reason = 'No host_and_port field provided'
                self.adapter_agent.update_device(device)
                return

            self.host_and_port = device.host_and_port
            device.root = True
            device.vendor = 'Edgecore'
            device.model = 'ASFvOLT16'
            device.serial_number = device.host_and_port
            self.adapter_agent.update_device(device)

            self.add_port(port_no=ASFVOLT_NNI_PORT,
                          port_type=Port.ETHERNET_NNI,
                          label='NNI facing Ethernet port')
            self.logical_device_id = \
                self.add_logical_device(device_id=device.id)
            self.add_logical_port(port_no=ASFVOLT_NNI_PORT,
                                  port_type=Port.ETHERNET_NNI,
                                  device_id=device.id,
                                  logical_device_id=self.logical_device_id)

            self.bal.connect_olt(device.host_and_port, self.device_id)

        self.bal.activate_olt()

        device = self.adapter_agent.get_device(device.id)
        device.parent_id = self.logical_device_id
        device.connect_status = ConnectStatus.REACHABLE
        device.oper_status = OperStatus.ACTIVATING
        self.adapter_agent.update_device(device)

    def add_port(self, port_no, port_type, label):
        self.log.info('adding-port', port_no=port_no, port_type=port_type)
        if port_type is Port.ETHERNET_NNI:
            oper_status = OperStatus.ACTIVE
        elif port_type is Port.PON_OLT:
            # To-Do The pon port status should be ACTIVATING.
            # For now make the status as Active.
            oper_status = OperStatus.ACTIVE
        else:
            self.log.error('invalid-port-type', port_type=port_type)
            return

        port = Port(
            port_no=port_no,
            label=label,
            type=port_type,
            admin_state=AdminState.ENABLED,
            oper_status=oper_status
        )
        self.adapter_agent.add_port(self.device_id, port)

    def add_logical_device(self, device_id):
        self.log.info('adding-logical-device', device_id=device_id)
        ld = LogicalDevice(
            # not setting id and datapth_id will let the adapter
            # agent pick id
            desc=ofp_desc(
                mfr_desc='cord project',
                hw_desc='n/a',
                sw_desc='logical device for Edgecore ASFvOLT16 OLT',
                serial_num=uuid4().hex,
                dp_desc='n/a'
            ),
            switch_features=ofp_switch_features(
                n_buffers=256,  # TODO fake for now
                n_tables=2,  # TODO ditto
                capabilities=(  # TODO and ditto
                    OFPC_FLOW_STATS |
                    OFPC_TABLE_STATS |
                    OFPC_PORT_STATS |
                    OFPC_GROUP_STATS
                )
            ),
            root_device_id=device_id
        )
        ld_initialized = self.adapter_agent.create_logical_device(ld)
        return ld_initialized.id

    def add_logical_port(self, port_no, port_type,
                         device_id, logical_device_id):
        self.log.info('adding-logical-port', port_no=port_no,
                      port_type=port_type, device_id=device_id)
        if port_type is Port.ETHERNET_NNI:
            label = 'nni'
            cap = OFPPF_1GB_FD | OFPPF_FIBER
            curr_speed = OFPPF_1GB_FD
            max_speed = OFPPF_1GB_FD
        else:
            self.log.error('invalid-port-type', port_type=port_type)
            return

        ofp = ofp_port(
            port_no=0,  # is 0 OK?
            hw_addr=mac_str_to_tuple('00:00:00:00:00:%02x' % 129),
            name=label,
            config=0,
            state=OFPPS_LIVE,
            curr=cap,
            advertised=cap,
            peer=cap,
            curr_speed=curr_speed,
            max_speed=max_speed)

        logical_port = LogicalPort(
            id=label,
            ofp_port=ofp,
            device_id=device_id,
            device_port_no=port_no,
            root_port=True
        )

        self.adapter_agent.add_logical_port(logical_device_id, logical_port)

    def handle_access_term_ind(self, ind_info):
        device = self.adapter_agent.get_device(self.device_id)
        if ind_info['activation_successful'] is True:
            self.log.info('successful-access-terminal-Indication',
                          olt_id=self.olt_id)
            device.connect_status = ConnectStatus.REACHABLE
            device.oper_status = OperStatus.ACTIVE
            device.reason = 'OLT activated successfully'
            self.adapter_agent.update_device(device)
            self.log.info('OLT activation complete')
        else:
            device.oper_status = OperStatus.FAILED
            device.reason = 'Failed to Intialize OLT'
            self.adapter_agent.update_device(device)
            reactor.callLater(15, self.activate, device)
        return

    def handle_not_started_onu(self, child_device, ind_info):
        if ind_info['_sub_group_type'] == 'onu_discovery':
            self.log.info('Onu-discovered', olt_id=self.olt_id,
                          pon_ni=ind_info['_pon_id'], onu_data=ind_info)
            # To-Do: Need to handle the ONUs, where the admin state is
            # ENABLED and operation state is in Failed or Unkown
            self.log.info('Not-Yet-handled', olt_id=self.olt_id,
                          pon_ni=ind_info['_pon_id'], onu_data=ind_info)
        else:
            self.log.info('Invalid-ONU-event', olt_id=self.olt_id,
                          pon_ni=ind_info['_pon_id'], onu_data=ind_info)

    def handle_activating_onu(self, child_device, ind_info):
        pon_id = ind_info['_pon_id']
        self.log.info('Not-handled-Yet', olt_id=self.olt_id,
                      pon_ni=pon_id, onu_data=ind_info)

    def handle_activated_onu(self, child_device, ind_info):
        pon_id = ind_info['_pon_id']
        self.log.info('Not-handled-Yet', olt_id=self.olt_id,
                      pon_ni=pon_id, onu_data=ind_info)

    def handle_discovered_onu(self, child_device, ind_info):
        pon_id = ind_info['_pon_id']
        if ind_info['_sub_group_type'] == 'onu_discovery':
            self.log.info('Activation-is-in-progress', olt_id=self.olt_id,
                          pon_ni=pon_id, onu_data=ind_info,
                          onu_id=child_device.proxy_address.onu_id)

        elif ind_info['_sub_group_type'] == 'sub_term_indication':
            self.log.info('ONU-activation-is-completed', olt_id=self.olt_id,
                          pon_ni=pon_id, onu_data=ind_info)

            msg = {'proxy_address': child_device.proxy_address,
                   'event': 'activation-completed', 'event_data': ind_info}

            # Send the event message to the ONU adapter
            self.adapter_agent.publish_inter_adapter_message(child_device.id,
                                                             msg)
            if ind_info['activation_successful'] is True:
                for key, v_ont_ani in self.v_ont_anis.items():
                    if v_ont_ani.v_ont_ani.data.onu_id == \
                            child_device.proxy_address.onu_id:
                        for tcont_key, tcont in v_ont_ani.tconts.items():
                            owner_info = dict()
                            # To-Do: Right Now use alloc_id as schduler ID. Need to
                            # find way to generate uninqe number.
                            id = tcont.alloc_id
                            owner_info['type'] = 'agg_port'
                            owner_info['intf_id'] = \
                                child_device.proxy_address.channel_id
                            owner_info['onu_id'] = \
                                child_device.proxy_address.onu_id
                            owner_info['alloc_id'] = tcont.alloc_id
                            self.bal.create_scheduler(id, 'upstream',
                                                      owner_info, 8)
        else:
            self.log.info('Invalid-ONU-event', olt_id=self.olt_id,
                          pon_ni=ind_info['_pon_id'], onu_data=ind_info)

    onu_handlers = {
        OperStatus.UNKNOWN: handle_not_started_onu,
        OperStatus.FAILED: handle_not_started_onu,
        OperStatus.ACTIVATING: handle_activating_onu,
        OperStatus.ACTIVE: handle_activated_onu,
        OperStatus.DISCOVERED: handle_discovered_onu,
    }

    def handle_sub_term_ind(self, ind_info):
        child_device = self.adapter_agent.get_child_device(
            self.device_id,
            serial_number=(ind_info['_vendor_id'] +
                           ind_info['_vendor_specific']))
        if child_device is None:
            self.log.info('Onu-is-not-configured', olt_id=self.olt_id,
                          pon_ni=ind_info['_pon_id'], onu_data=ind_info)
            return

        handler = self.onu_handlers.get(child_device.oper_status)
        if handler:
            handler(self, child_device, ind_info)

    def send_proxied_message(self, proxy_address, msg):
        if isinstance(msg, Packet):
            msg = str(msg)
        try:
            self.bal.send_omci_request_message(proxy_address, msg)
        except Exception as e:
            self.log.exception('', exc=str(e))
        return

    def handle_omci_ind(self, ind_info):
        child_device = self.adapter_agent.get_child_device(
            self.device_id,
            onu_id=ind_info['onu_id'])
        if child_device is None:
            self.log.info('Onu is not configured', onu_id=ind_info['onu_id'])
            return
        try:
            self.adapter_agent.receive_proxied_message(
                child_device.proxy_address,
                ind_info['packet'])
        except Exception as e:
            self.log.exception('', exc=str(e))
        return

    def handle_v_ont_ani_config(self, data):
        serial_number = data.data.expected_serial_number
        child_device = self.adapter_agent.get_child_device(
            self.device_id,
            serial_number=serial_number)
        if child_device is None:
            self.log.info('Failed-to-find-ONU-Info',
                          serial_number=serial_number)
        elif child_device.admin_state == AdminState.ENABLED:
            self.log.info('Activating ONU',
                          serial_number=serial_number,
                          onu_id=child_device.proxy_address.onu_id,
                          pon_id=child_device.parent_port_no)
            onu_info = dict()
            onu_info['pon_id'] = child_device.parent_port_no
            onu_info['onu_id'] = child_device.proxy_address.onu_id
            onu_info['vendor'] = child_device.vendor_id
            onu_info['vendor_specific'] = serial_number[4:]
            self.bal.activate_onu(onu_info)
        else:
            self.log.info('Invalid-ONU-state-to-activate',
                          onu_id=child_device.proxy_address.onu_id,
                          serial_number=serial_number)

    def create_interface(self, data):
        try:
            if isinstance(data, ChannelgroupConfig):
                if data.name in self.channel_groups:
                    self.log('Channel-Group-already-present',
                             channel_group=data)
                else:
                    channel_group_config = ChannelgroupConfig()
                    channel_group_config.CopyFrom(data)
                    self.channel_groups[data.name] = channel_group_config
            if isinstance(data, ChannelpartitionConfig):
                if data.name in self.channel_partitions:
                    self.log('Channel-partition-already-present',
                             channel_partition=data)
                else:
                    channel_partition_config = ChannelpartitionConfig()
                    channel_partition_config.CopyFrom(data)
                    self.channel_partitions[data.name] = \
                        channel_partition_config
            if isinstance(data, ChannelpairConfig):
                if data.name in self.channel_pairs:
                    self.log('Channel-pair-already-present',
                             channel_pair=data)
                else:
                    channel_pair_config = ChannelpairConfig()
                    channel_pair_config.CopyFrom(data)
                    self.channel_pairs[data.name] = channel_pair_config
            if isinstance(data, ChannelterminationConfig):
                self.log.info('Activating-PON-port-at-OLT',
                              pon_id=data.data.xgs_ponid)
                self.add_port(port_no=data.data.xgs_ponid,
                              port_type=Port.PON_OLT,
                              label=data.name)
                self.bal.activate_pon_port(self.olt_id, data.data.xgs_ponid)
                if data.name in self.channel_terminations:
                    self.log.info('Channel-termination-already-present',
                                  channel_termination=data)
                else:
                    channel_termination_config = ChannelterminationConfig()
                    channel_termination_config.CopyFrom(data)
                    self.channel_terminations[data.name] = \
                        channel_termination_config
            if isinstance(data, VOntaniConfig):
                if data.name in self.v_ont_anis:
                    self.log.info('v_ont_ani-already-present',
                                  v_ont_ani=data)
                else:
                    self.handle_v_ont_ani_config(data)
                    v_ont_ani_config = VOntAniHandler()
                    v_ont_ani_config.v_ont_ani.CopyFrom(data)
                    self.v_ont_anis[data.name] = v_ont_ani_config
            if isinstance(data, VEnetConfig):
                if data.name in self.v_enets:
                    self.log.info('v_enet-already-present',
                                  v_enet=data)
                else:
                    v_enet_config = VEnetHandler()
                    v_enet_config.v_enet.CopyFrom(data)
                    self.log.info("creating-port-at-olt")
                    self.adapter_agent.add_port(self.device_id, Port(
                        port_no=self._get_next_uni_port(),
                        label=data.interface.name,
                        type=Port.ETHERNET_UNI,
                        admin_state=AdminState.ENABLED,
                        oper_status=OperStatus.ACTIVE
                    ))
                    self.v_enets[data.name] = v_enet_config
            if isinstance(data, OntaniConfig):
                if data.name in self.ont_anis:
                    self.log.info('ont_ani-already-present',
                                  v_enet=data)
                else:
                    ont_ani_config = OntaniConfig()
                    ont_ani_config.CopyFrom(data)
                    self.ont_anis[data.name] = ont_ani_config
        except Exception as e:
            self.log.exception('', exc=str(e))
        return

    def update_interface(self, data):
        self.log.info('Not-Implemented-yet')
        return

    def remove_interface(self, data):
        self.log.info('Not-Implemented-yet')
        return

    def create_tcont(self, tcont_data, traffic_descriptor_data):
        if traffic_descriptor_data.name in self.traffic_descriptors:
            traffic_descriptor = TrafficDescriptorProfileData()
            traffic_descriptor.CopyFrom(traffic_descriptor_data)
            self.traffic_descriptors[traffic_descriptor_data.name] = \
                traffic_descriptor
        if tcont_data.interface_reference in self.v_ont_anis:
            v_ont_ani = self.v_ont_anis[tcont_data.interface_reference]
            onu_device = self.adapter_agent.get_child_device(
                self.device_id,
                onu_id=v_ont_ani.v_ont_ani.data.onu_id)
            if (onu_device is not None and
                        onu_device.oper_status == OperStatus.ACTIVE):
                owner_info = dict()
                # To-Do: Right Now use alloc_id as schduler ID. Need to
                # find way to generate uninqe number.
                id = tcont_data.alloc_id
                owner_info['type'] = 'agg_port'
                owner_info['intf_id'] = onu_device.proxy_address.channel_id
                owner_info['onu_id'] = onu_device.proxy_address.onu_id
                owner_info['alloc_id'] = tcont_data.alloc_id
                self.bal.create_scheduler(id, 'upstream', owner_info, 8)
            else:
                self.log.info('Onu-is-not-configured', olt_id=self.olt_id,
                              intf_id=onu_device.proxy_address.channel_id,
                              onu_data=onu_device.proxy_address.onu_id)
            if tcont_data.name in v_ont_ani.tconts:
                self.log.info('tcont-info-already-present',
                              tcont_info=tcont_data)
            else:
                tcont = TcontsConfigData()
                tcont.CopyFrom(tcont_data)
                v_ont_ani.tconts[tcont_data.name] = tcont

    def update_tcont(self, tcont_data, traffic_descriptor_data):
        raise NotImplementedError()

    def remove_tcont(self, tcont_data, traffic_descriptor_data):
        raise NotImplementedError()

    def create_gemport(self, data):
        if data.itf_ref in self.v_enets:
            v_enet = self.v_enets[data.itf_ref]
            if data.name in v_enet.gem_ports:
                self.log.info('Gem-port-info-is-already-present',
                              VEnet=v_enet, gem_info=data)
            if data.gemport_id > 9212:
                raise Exception('supported range for '
                                'gem-port is from 1024 to 4099')
            gem_port = GemportsConfigData()
            gem_port.CopyFrom(data)
            v_enet.gem_ports[data.name] = gem_port
        else:
            self.log.info('VEnet-is-not-configured-yet.',
                          gem_port_info=data)

    def update_gemport(self, data):
        raise NotImplementedError()

    def remove_gemport(self, data):
        raise NotImplementedError()

    def disable(self):
        super(Asfvolt16Handler, self).disable()

    def delete(self):
        super(Asfvolt16Handler, self).delete()

    def handle_packet_in(self, ind_info):
        self.log.info('Received Packet-In', ind_info=ind_info)

        pkt = Ether(ind_info['packet'])
        if pkt.haslayer(Dot1Q):
            outer_shim = pkt.getlayer(Dot1Q)
            if isinstance(outer_shim.payload, Dot1Q):
                inner_shim = outer_shim.payload
                cvid = inner_shim.vlan
                logical_port = cvid
                popped_frame = (
                    Ether(src=pkt.src, dst=pkt.dst, type=inner_shim.type) /
                    inner_shim.payload
                )
                kw = dict(
                    logical_device_id=self.logical_device_id,
                    logical_port_no=logical_port,
                )
                self.log.info('sending-packet-in', **kw)
                self.adapter_agent.send_packet_in(
                    packet=str(popped_frame), **kw)

        reactor.callLater(1, self.process_packet_in)

    def packet_out(self, egress_port, msg):
        self.log.info('sending-packet-out', egress_port=egress_port,
                      msg=hexify(msg))
        pkt = Ether(msg)
        out_pkt = (
            Ether(src=pkt.src, dst=pkt.dst) /
            Dot1Q(vlan=PACKET_IN_VLAN) /
            Dot1Q(vlan=egress_port, type=pkt.type) /
            pkt.payload
        )
        onu_id = 1
        # TODO: Need to retrieve the correct destination onu_id
        self.bal.packet_out(onu_id, egress_port, str(out_pkt))

    def update_flow_table(self, flows):
        device = self.adapter_agent.get_device(self.device_id)
        self.log.info('bulk-flow-update', device_id=self.device_id)

        for flow in flows:
            self.log.info('flow-details', device_id=self.device_id, flow=flow)
            classifier_info = dict()
            action_info = dict()
            is_down_stream = None
            _in_port = None
            try:
                _in_port = fd.get_in_port(flow)
                assert _in_port is not None
                # Right now there is only one NNI port. Get the NNI PORT and compare
                # with IN_PUT port number. Need to find better way.
                ports = self.adapter_agent.get_ports(device.id, Port.ETHERNET_NNI)

                for port in ports:
                    if (port.port_no == _in_port):
                        self.log.info('downstream-flow')
                        is_down_stream = True
                        break
                if is_down_stream is None:
                    is_down_stream = False
                    self.log.info('upstream-flow')

                _out_port = fd.get_out_port(flow)  # may be None
                self.log.info('out-port', out_port=_out_port)

                for field in fd.get_ofb_fields(flow):

                    if field.type == fd.ETH_TYPE:
                        classifier_info['eth_type'] = field.eth_type
                        self.log.info('field-type-eth-type',
                                      eth_type=classifier_info['eth_type'])

                    elif field.type == fd.IP_PROTO:
                        classifier_info['ip_proto'] = field.ip_proto
                        self.log.info('field-type-ip-proto',
                                      ip_proto=classifier_info['ip_proto'])

                    elif field.type == fd.IN_PORT:
                        classifier_info['in_port'] = field.port
                        self.log.info('field-type-in-port',
                                      in_port=classifier_info['in_port'])

                    elif field.type == fd.VLAN_VID:
                        classifier_info['vlan_vid'] = field.vlan_vid & 0xfff
                        self.log.info('field-type-vlan-vid',
                                      vlan=classifier_info['vlan_vid'])

                    elif field.type == fd.VLAN_PCP:
                        classifier_info['vlan_pcp'] = field.vlan_pcp
                        self.log.info('field-type-vlan-pcp',
                                      pcp=classifier_info['vlan_pcp'])

                    elif field.type == fd.UDP_DST:
                        classifier_info['udp_dst'] = field.udp_dst
                        self.log.info('field-type-udp-dst',
                                      udp_dst=classifier_info['udp_dst'])

                    elif field.type == fd.UDP_SRC:
                        classifier_info['udp_src'] = field.udp_src
                        self.log.info('field-type-udp-src',
                                      udp_src=classifier_info['udp_src'])

                    elif field.type == fd.IPV4_DST:
                        classifier_info['ipv4_dst'] = field.ipv4_dst
                        self.log.info('field-type-ipv4-dst',
                                      ipv4_dst=classifier_info['ipv4_dst'])

                    elif field.type == fd.IPV4_SRC:
                        classifier_info['ipv4_src'] = field.ipv4_src
                        self.log.info('field-type-ipv4-src',
                                      ipv4_dst=classifier_info['ipv4_src'])

                    elif field.type == fd.METADATA:
                        classifier_info['metadata'] = field.table_metadata
                        self.log.info('field-type-metadata',
                                      metadata=classifier_info['metadata'])

                    else:
                        raise NotImplementedError('field.type={}'.format(
                            field.type))

                for action in fd.get_actions(flow):

                    if action.type == fd.OUTPUT:
                        action_info['output'] = action.output.port
                        self.log.info('action-type-output',
                                      output=action_info['output'],
                                      in_port=classifier_info['in_port'])

                    elif action.type == fd.POP_VLAN:
                        action_info['pop_vlan'] = True
                        self.log.info('action-type-pop-vlan',
                                      in_port=_in_port)

                    elif action.type == fd.PUSH_VLAN:
                        action_info['push_vlan'] = True
                        action_info['tpid'] = action.push.ethertype
                        self.log.info('action-type-push-vlan',
                                      push_tpid=action_info['tpid'],
                                      in_port=_in_port)
                        if action.push.ethertype != 0x8100:
                            self.log.error('unhandled-tpid',
                                           ethertype=action.push.ethertype)

                    elif action.type == fd.SET_FIELD:
                        # action_info['action_type'] = 'set_field'
                        _field = action.set_field.field.ofb_field
                        assert (action.set_field.field.oxm_class ==
                                OFPXMC_OPENFLOW_BASIC)
                        self.log.info('action-type-set-field',
                                      field=_field, in_port=_in_port)
                        if _field.type == fd.VLAN_VID:
                            self.log.info('set-field-type-vlan-vid',
                                          vlan_vid=_field.vlan_vid & 0xfff)
                            action_info['vlan_vid'] = (_field.vlan_vid & 0xfff)
                        else:
                            self.log.error('unsupported-action-set-field-type',
                                           field_type=_field.type)
                    else:
                        self.log.error('unsupported-action-type',
                                       action_type=action.type, in_port=_in_port)

                if is_down_stream is False:
                    self.divide_and_add_flow(classifier_info, action_info)
            except Exception as e:
                self.log.exception('failed-to-install-flow', e=e, flow=flow)

    # This function will divide the upstream flow into both
    # upstreand and downstream flow, as broadcom devices
    # expects down stream flows to be added to handle
    # packet_out messge from controller.
    def divide_and_add_flow(self, classifier, action):
        found = False
        ports = self.adapter_agent.get_ports(self.device_id, Port.ETHERNET_UNI)
        for port in ports:
            if port.port_no == classifier['in_port']:
                found = True
                break
        if found is True:
            v_enet = self.get_venet(port.label)
        else:
            self.log.error('Failed to get v_enet info',
                           in_port=classifier['in_port'])
            return
        if 'ip_proto' in classifier:
            if classifier['ip_proto'] == 17:
                self.log.error('Addtion of DHCP flows are defferd')
                '''
                # DHCP flow from the ONOS doesn't have Ctag and Stags
                # information. For now DHCP flow will be added as a
                # part of data flows.
                # self.add_dhcp_flow(classifier, action, v_enet,
                #                   ASFVOLT_DHCP_UNTAGGED_ID)
                '''
            elif classifier['ip_proto'] == 2:
                self.log.info('Addtion of IGMP flow are not handled yet')
                '''
                #self.add_igmp_flow(classifier, action, v_enet,
                #                   ASFVOLT_IGMP_UNTAGGED_ID)
                '''
            else:
                self.log.info("Invalid-Classifier-to-handle",
                              classifier=classifier,
                              action=action)
        elif 'eth_type' in classifier:
            if classifier['eth_type'] == 0x888e:
                # self.log.error('Addtion of EAPOL flows are defferd')
                self.add_eapol_flow(classifier, action,
                                    v_enet, ASFVOLT_EAPOL_ID,
                                    ASFVOLT_DOWNLINK_EAPOL_ID)
        elif 'push_vlan' in action:
            self.prepare_and_add_dhcp_flow(classifier, action, v_enet,
                                           ASFVOLT_DHCP_TAGGED_ID,
                                           ASFVOLT_DOWNLINK_DHCP_TAGGED_ID)
            self.add_data_flow(classifier, action, v_enet)
        else:
            self.log.info('Invalid-flow-type-to-handle',
                          classifier=classifier,
                          action=action)

    def add_eapol_flow(self, uplink_classifier, uplink_action,
                       v_enet, uplink_eapol_id, downlink_eapol_id):
        downlink_classifier = dict(uplink_classifier)
        downlink_action = dict(uplink_action)
        # To-Do For a time being hard code the traffic class value.
        # Need to know how to get the traffic class info from flows.
        gem_port = self.get_gem_port_info(v_enet, traffic_class=2)
        if gem_port is None:
            self.log.info('Failed-to-get-gemport',)
            return
        v_ont_ani = self.get_v_ont_ani(v_enet.v_enet.data.v_ontani_ref)
        if v_ont_ani is None:
            self.log.info('Failed-to-get-v_ont_ani',
                          v_ont_ani=v_enet.v_enet.data.v_ontani_ref)
            return
        tcont = self.get_tcont_info(v_ont_ani, name=gem_port.tcont_ref)
        if tcont is None:
            self.log.info('Failed-to-get-tcont-info',
                          tcont=gem_port.tcont_ref)
            return
        onu_device = self.adapter_agent.get_child_device(
            self.device_id, onu_id=v_ont_ani.v_ont_ani.data.onu_id)
        if onu_device is None:
            self.log.info('Failed-to-get-onu-device',
                          onu_id=v_ont_ani.v_ont_ani.data.onu_id)
            return

        flow_id = self.get_flow_id(onu_device.proxy_address.onu_id,
                                   onu_device.proxy_address.channel_id,
                                   uplink_eapol_id)
        # Add Upstream EAPOL Flow.
        uplink_classifier['pkt_tag_type'] = 'untagged'
        uplink_action.clear()
        uplink_action['trap_to_host'] = True
        try:
            is_down_stream = False
            self.log.info('Adding-Upstream-EAPOL-flow',
                          classifier=uplink_classifier,
                          action=uplink_action, gem_port=gem_port,
                          flow_id=flow_id,
                          sched_info=tcont.alloc_id)
            self.bal.add_flow(onu_device.proxy_address.onu_id,
                              onu_device.proxy_address.channel_id,
                              flow_id, gem_port.gemport_id,
                              uplink_classifier, is_down_stream,
                              action_info=uplink_action,
                              sched_id=tcont.alloc_id)
            # To-Do. While addition of one flow is in progress,
            # we cannot add an another flow. Right now use sleep
            # of 5 sec, assuming that addtion of flow is successful.
            time.sleep(10)
        except Exception as e:
            self.log.exception('failed-to-install-Upstream-EAPOL-flow', e=e,
                               classifier=uplink_classifier,
                               action=uplink_action,
                               onu_id=onu_device.proxy_address.onu_id,
                               intf_id=onu_device.proxy_address.channel_id)

        # Add Downstream EAPOL Flow.
        downlink_flow_id = self.get_flow_id(onu_device.proxy_address.onu_id,
                                            onu_device.proxy_address.channel_id,
                                            downlink_eapol_id)
        is_down_stream = True
        downlink_classifier['pkt_tag_type'] = 'untagged'
        try:
            self.log.info('Adding-Downstream-EAPOL-flow',
                          classifier=downlink_classifier,
                          action=downlink_action,
                          gem_port=gem_port,
                          flow_id=downlink_flow_id,
                          sched_info=tcont.alloc_id)
            self.bal.add_flow(onu_device.proxy_address.onu_id,
                              onu_device.proxy_address.channel_id,
                              downlink_flow_id, gem_port.gemport_id,
                              downlink_classifier, is_down_stream)
            # To-Do. While addition of one flow is in progress,
            # we cannot add an another flow. Right now use sleep
            # of 5 sec, assuming that addtion of flow is successful.
            time.sleep(10)
        except Exception as e:
            self.log.exception('failed-to-install-downstream-EAPOL-flow', e=e,
                               classifier=downlink_classifier,
                               action=downlink_action,
                               onu_id=onu_device.proxy_address.onu_id,
                               intf_id=onu_device.proxy_address.channel_id)

    def prepare_and_add_dhcp_flow(self, data_classifier, data_action,
                                  v_enet, dhcp_id, downlink_dhcp_id):
        dhcp_classifier = dict()
        dhcp_action = dict()
        dhcp_classifier['ip_proto'] = 17
        dhcp_classifier['udp_src'] = 68
        dhcp_classifier['udp_dsct'] = 67
        dhcp_classifier['pkt_tag_type'] = 'single_tag'
        dhcp_classifier['vlan_vid'] = data_classifier['vlan_vid']
        dhcp_action['vlan_push'] = True
        dhcp_action['vlan_vid'] = data_action['vlan_vid']
        self.add_dhcp_flow(dhcp_classifier, dhcp_action, v_enet,
                           dhcp_id, downlink_dhcp_id)

    def add_dhcp_flow(self, uplink_classifier, uplink_action,
                      v_enet, dhcp_id, downlink_dhcp_id):
        downlink_classifier = dict(uplink_classifier)
        downlink_action = dict(uplink_action)
        # Add Upstream DHCP Flow.
        # To-Do For a time being hard code the traffic class value.
        # Need to know how to get the traffic class info from flows.
        gem_port = self.get_gem_port_info(v_enet, traffic_class=2)
        if gem_port is None:
            self.log.error('Failed-to-get-gemport')
            return
        v_ont_ani = self.get_v_ont_ani(v_enet.v_enet.data.v_ontani_ref)
        if v_ont_ani is None:
            self.log.error('Failed-to-get-v_ont_ani',
                           v_ont_ani=v_enet.v_enet.data.v_ontani_ref)
            return
        tcont = self.get_tcont_info(v_ont_ani, name=gem_port.tcont_ref)
        if tcont is None:
            self.log.error('Failed-to-get-tcont-info',
                           tcont=gem_port.tcont_ref)
            return
        onu_device = self.adapter_agent.get_child_device(
            self.device_id, onu_id=v_ont_ani.v_ont_ani.data.onu_id)
        if onu_device is None:
            self.log.error('Failed-to-get-onu-device',
                           onu_id=v_ont_ani.v_ont_ani.data.onu_id)
            return

        flow_id = self.get_flow_id(onu_device.proxy_address.onu_id,
                                   onu_device.proxy_address.channel_id,
                                   dhcp_id)
        uplink_action.clear()
        uplink_action['trap_to_host'] = True
        try:
            is_down_stream = False
            self.log.info('Adding-Upstream-DHCP-flow',
                          classifier=uplink_classifier,
                          action=uplink_action,
                          gem_port=gem_port,
                          flow_id=flow_id,
                          sched_info=tcont.alloc_id)
            self.bal.add_flow(onu_device.proxy_address.onu_id,
                              onu_device.proxy_address.channel_id,
                              flow_id, gem_port.gemport_id,
                              uplink_classifier, is_down_stream,
                              action_info=uplink_action,
                              sched_id=tcont.alloc_id)
            # To-Do. While addition of one flow is in progress,
            # we cannot add an another flow. Right now use sleep
            # of 5 sec, assuming that addtion of flow is successful.
            time.sleep(10)

        except Exception as e:
            self.log.exception('failed-to-install-dhcp-upstream-flow', e=e,
                               classifier=uplink_classifier,
                               action=uplink_action,
                               onu_id=onu_device.proxy_address.onu_id,
                               intf_id=onu_device.proxy_address.channel_id)

        is_down_stream = True
        downlink_classifier['udp_src'] = 67
        downlink_classifier['udp_dst'] = 68

        if dhcp_id == ASFVOLT_DHCP_TAGGED_ID:
            downlink_classifier['pkt_tag_type'] = 'double_tag'
            downlink_classifier['vlan_vid'] = downlink_action['vlan_vid']
            if 'push_vlan' in downlink_classifier:
                downlink_action.pop('push_vlan')
            downlink_action['pop_vlan'] = True

        downlink_flow_id = self.get_flow_id(onu_device.proxy_address.onu_id,
                                            onu_device.proxy_address.channel_id,
                                            downlink_dhcp_id)

        try:
            self.log.info('Adding-Downstream-DHCP-flow',
                          classifier=downlink_classifier,
                          action=downlink_action, gem_port=gem_port,
                          flow_id=downlink_flow_id,
                          sched_info=tcont.alloc_id)
            self.bal.add_flow(onu_device.proxy_address.onu_id,
                              onu_device.proxy_address.channel_id,
                              downlink_flow_id, gem_port.gemport_id,
                              downlink_classifier, is_down_stream,
                              action_info=downlink_action)
            # To-Do. While addition of one flow is in progress,
            # we cannot add an another flow. Right now use sleep
            # of 5 sec, assuming that addtion of flow is successful.
            time.sleep(10)
        except Exception as e:
            self.log.exception('failed-to-install-dhcp-downstream-flow', e=e,
                               classifier=downlink_classifier,
                               action=downlink_action,
                               onu_id=onu_device.proxy_address.onu_id,
                               intf_id=onu_device.proxy_address.channel_id)

    def add_igmp_flow(self, classifier, action, v_enet, igmp_id):
        self.log.info('Not-Implemented-Yet')
        return

    def add_data_flow(self, uplink_classifier, uplink_action, v_enet):

        downlink_classifier = dict(uplink_classifier)
        downlink_action = dict(uplink_action)

        uplink_classifier['pkt_tag_type'] = 'single_tag'

        downlink_classifier['pkt_tag_type'] = 'double_tag'
        downlink_classifier['vlan_vid'] = uplink_action['vlan_vid']
        downlink_classifier['vlan_pcp'] = 0
        del downlink_action['push_vlan']
        downlink_action['pop_vlan'] = True

        self.add_firmware_flow(uplink_classifier, uplink_action,
                               downlink_classifier, downlink_action,
                               v_enet, ASFVOLT_FIRMWARE_ID,
                               ASFVOLT_DOWNLINK_FIRMWARE_ID)
        self.add_arp_flow(uplink_classifier, uplink_action,
                          downlink_classifier, downlink_action,
                          v_enet, ASFVOLT_ARP_ID, ASFVOLT_DOWNLINK_ARP_ID)
        self.add_dns_flow(uplink_classifier, uplink_action,
                          downlink_classifier, downlink_action,
                          v_enet, ASFVOLT_DNS_ID, ASFVOLT_DOWNLINK_DNS_ID)
        self.add_hsia_flow(uplink_classifier, uplink_action,
                           downlink_classifier, downlink_action,
                           v_enet, ASFVOLT_HSIA_ID, ASFVOLT_DOWNLINK_HSIA_ID)

    def add_firmware_flow(self, uplink_classifier, uplink_action,
                          downlink_classifier, downlink_action,
                          v_enet, firmware_id, downlink_firmware_id):
        # Add Upstream Firmware Flow.
        # To-Do For a time being hard code the traffic class value.
        # Need to know how to get the traffic class info from flows.
        gem_port = self.get_gem_port_info(v_enet, traffic_class=2)
        if gem_port is None:
            self.log.info('Failed-to-get-gemport')
            return
        v_ont_ani = self.get_v_ont_ani(v_enet.v_enet.data.v_ontani_ref)
        if v_ont_ani is None:
            self.log.info('Failed-to-get-v_ont_ani',
                          v_ont_ani=v_enet.v_enet.data.v_ontani_ref)
            return
        tcont = self.get_tcont_info(v_ont_ani, name=gem_port.tcont_ref)
        if tcont is None:
            self.log.info('Failed-to-get-tcont-info',
                          tcont=gem_port.tcont_ref)
            return
        onu_device = self.adapter_agent.get_child_device(
            self.device_id, onu_id=v_ont_ani.v_ont_ani.data.onu_id)
        if onu_device is None:
            self.log.info('Failed-to-get-onu-device',
                          onu_id=v_ont_ani.v_ont_ani.data.onu_id)
            return

        flow_id = self.get_flow_id(onu_device.proxy_address.onu_id,
                                   onu_device.proxy_address.channel_id,
                                   firmware_id)
        self.log.info('Adding-FirmWare-data-upstream-flow')
        try:
            is_down_stream = False
            self.log.info('Adding-Firmware-data-upstream-flow',
                          classifier=uplink_classifier,
                          action=uplink_action,
                          gem_port=gem_port,
                          flow_id=flow_id,
                          sched_info=tcont.alloc_id)
            self.bal.add_flow(onu_device.proxy_address.onu_id,
                              onu_device.proxy_address.channel_id,
                              flow_id, gem_port.gemport_id,
                              uplink_classifier, is_down_stream,
                              action_info=uplink_action,
                              sched_id=tcont.alloc_id)
            # To-Do. While addition of one flow is in progress,
            # we cannot add an another flow. Right now use sleep
            # of 5 sec, assuming that addtion of flow is successful.
            time.sleep(10)
        except Exception as e:
            self.log.exception('failed-to-install-firmware-upstream-flow', e=e,
                               classifier=uplink_classifier,
                               action=uplink_action,
                               onu_id=onu_device.proxy_address.onu_id,
                               intf_id=onu_device.proxy_address.channel_id)
        is_down_stream = True
        # To-Do: For Now hard code the p-bit values.
        downlink_classifier['vlan_pcp'] = 1
        downlink_flow_id = self.get_flow_id(onu_device.proxy_address.onu_id,
                                            onu_device.proxy_address.channel_id,
                                            downlink_firmware_id)
        try:
            self.log.info('Adding-Firmware-data-downstream-flow',
                          classifier=downlink_classifier,
                          action=downlink_action,
                          gem_port=gem_port,
                          flow_id=downlink_flow_id)
            self.bal.add_flow(onu_device.proxy_address.onu_id,
                              onu_device.proxy_address.channel_id,
                              downlink_flow_id, gem_port.gemport_id,
                              downlink_classifier, is_down_stream,
                              action_info=downlink_action)
            # To-Do. While addition of one flow is in progress,
            # we cannot add an another flow. Right now use sleep
            # of 5 sec, assuming that addtion of flow is successful.
            time.sleep(10)
        except Exception as e:
            self.log.exception('failed-to-install-firmware-downstream-flow', e=e,
                               classifier=downlink_classifier,
                               action=downlink_action,
                               onu_id=onu_device.proxy_address.onu_id,
                               intf_id=onu_device.proxy_address.channel_id)

    def add_dns_flow(self, uplink_classifier, uplink_action,
                     downlink_classifier, downlink_action,
                     v_enet, dns_id, downlink_dns_id):
        # Add Upstream Firmware Flow.
        # To-Do For a time being hard code the traffic class value.
        # Need to know how to get the traffic class info from flows.
        gem_port = self.get_gem_port_info(v_enet, traffic_class=2)
        if gem_port is None:
            self.log.info('Failed-to-get-gemport')
            return
        v_ont_ani = self.get_v_ont_ani(v_enet.v_enet.data.v_ontani_ref)
        if v_ont_ani is None:
            self.log.info('Failed-to-get-v_ont_ani',
                          v_ont_ani=v_enet.v_enet.data.v_ontani_ref)
            return
        tcont = self.get_tcont_info(v_ont_ani, name=gem_port.tcont_ref)
        if tcont is None:
            self.log.info('Failed-to-get-tcont-info',
                          tcont=gem_port.tcont_ref)
            return
        onu_device = self.adapter_agent.get_child_device(
            self.device_id, onu_id=v_ont_ani.v_ont_ani.data.onu_id)
        if onu_device is None:
            self.log.info('Failed-to-get-onu-device',
                          onu_id=v_ont_ani.v_ont_ani.data.onu_id)
            return

        flow_id = self.get_flow_id(onu_device.proxy_address.onu_id,
                                   onu_device.proxy_address.channel_id,
                                   dns_id)
        try:
            is_down_stream = False
            self.log.info('Adding-DNS-upstream-flow',
                          classifier=uplink_classifier,
                          action=uplink_action,
                          gem_port=gem_port,
                          flow_id=flow_id,
                          sched_info=tcont.alloc_id)
            self.bal.add_flow(onu_device.proxy_address.onu_id,
                              onu_device.proxy_address.channel_id,
                              flow_id, gem_port.gemport_id,
                              uplink_classifier, is_down_stream,
                              action_info=uplink_action,
                              sched_id=tcont.alloc_id)
            # To-Do. While addition of one flow is in progress,
            # we cannot add an another flow. Right now use sleep
            # of 5 sec, assuming that addtion of flow is successful.
            time.sleep(10)
        except Exception as e:
            self.log.exception('failed-to-install-DNS-upstream-flow', e=e,
                               classifier=uplink_classifier,
                               action=uplink_action,
                               onu_id=onu_device.proxy_address.onu_id,
                               intf_id=onu_device.proxy_address.channel_id)
        is_down_stream = True
        # To-Do: For Now hard code the p-bit values.
        downlink_classifier['vlan_pcp'] = 3
        downlink_flow_id = self.get_flow_id(onu_device.proxy_address.onu_id,
                                            onu_device.proxy_address.channel_id,
                                            downlink_dns_id)
        try:
            self.log.info('Adding-DNS-downstream-flow',
                          classifier=downlink_classifier,
                          action=downlink_action,
                          gem_port=gem_port,
                          flow_id=downlink_flow_id)
            self.bal.add_flow(onu_device.proxy_address.onu_id,
                              onu_device.proxy_address.channel_id,
                              downlink_flow_id, gem_port.gemport_id,
                              downlink_classifier, is_down_stream,
                              action_info=downlink_action)
            # To-Do. While addition of one flow is in progress,
            # we cannot add an another flow. Right now use sleep
            # of 5 sec, assuming that addtion of flow is successful.
            time.sleep(10)
        except Exception as e:
            self.log.exception('failed-to-install-DNS-downstream-flow', e=e,
                               classifier=downlink_classifier,
                               action=downlink_action,
                               onu_id=onu_device.proxy_address.onu_id,
                               intf_id=onu_device.proxy_address.channel_id)

    def add_arp_flow(self, uplink_classifier, uplink_action,
                     downlink_classifier, downlink_action,
                     v_enet, arp_id, downlink_arp_id):
        # Add Upstream Firmware Flow.
        # To-Do For a time being hard code the traffic class value.
        # Need to know how to get the traffic class info from flows.
        gem_port = self.get_gem_port_info(v_enet, traffic_class=2)
        if gem_port is None:
            self.log.info('Failed-to-get-gemport')
            return
        v_ont_ani = self.get_v_ont_ani(v_enet.v_enet.data.v_ontani_ref)
        if v_ont_ani is None:
            self.log.info('Failed-to-get-v_ont_ani',
                          v_ont_ani=v_enet.v_enet.data.v_ontani_ref)
            return
        tcont = self.get_tcont_info(v_ont_ani, name=gem_port.tcont_ref)
        if tcont is None:
            self.log.info('Failed-to-get-tcont-info',
                          tcont=gem_port.tcont_ref)
            return
        onu_device = self.adapter_agent.get_child_device(
            self.device_id, onu_id=v_ont_ani.v_ont_ani.data.onu_id)
        if onu_device is None:
            self.log.info('Failed-to-get-onu-device',
                          onu_id=v_ont_ani.v_ont_ani.data.onu_id)
            return

        flow_id = self.get_flow_id(onu_device.proxy_address.onu_id,
                                   onu_device.proxy_address.channel_id,
                                   arp_id)
        try:
            is_down_stream = False
            self.log.info('Adding-ARP-upstream-flow',
                          classifier=uplink_classifier,
                          action=uplink_action,
                          gem_port=gem_port,
                          flow_id=flow_id,
                          sched_info=tcont.alloc_id)
            self.bal.add_flow(onu_device.proxy_address.onu_id,
                              onu_device.proxy_address.channel_id,
                              flow_id, gem_port.gemport_id,
                              uplink_classifier, is_down_stream,
                              action_info=uplink_action,
                              sched_id=tcont.alloc_id)
            # To-Do. While addition of one flow is in progress,
            # we cannot add an another flow. Right now use sleep
            # of 5 sec, assuming that addtion of flow is successful.
            time.sleep(10)
        except Exception as e:
            self.log.exception('failed-to-install-ARP-upstream-flow', e=e,
                               classifier=uplink_classifier,
                               action=uplink_action,
                               onu_id=onu_device.proxy_address.onu_id,
                               intf_id=onu_device.proxy_address.channel_id)
        is_down_stream = True
        # To-Do: For Now hard code the p-bit values.
        downlink_classifier['vlan_pcp'] = 7
        downlink_flow_id = self.get_flow_id(onu_device.proxy_address.onu_id,
                                            onu_device.proxy_address.channel_id,
                                            downlink_arp_id)
        try:
            self.log.info('Adding-ARP-downstream-flow',
                          classifier=downlink_classifier,
                          action=downlink_action,
                          gem_port=gem_port,
                          flow_id=downlink_flow_id)
            self.bal.add_flow(onu_device.proxy_address.onu_id,
                              onu_device.proxy_address.channel_id,
                              downlink_flow_id, gem_port.gemport_id,
                              downlink_classifier, is_down_stream,
                              action_info=downlink_action)
            # To-Do. While addition of one flow is in progress,
            # we cannot add an another flow. Right now use sleep
            # of 5 sec, assuming that addtion of flow is successful.
            time.sleep(10)
        except Exception as e:
            self.log.exception('failed-to-install-ARP-downstream-flow', e=e,
                               classifier=downlink_classifier,
                               action=downlink_action,
                               onu_id=onu_device.proxy_address.onu_id,
                               intf_id=onu_device.proxy_address.channel_id)

    def add_hsia_flow(self, uplink_classifier, uplink_action,
                      downlink_classifier, downlink_action,
                      v_enet, hsia_id, downlink_hsia_id):
        # Add Upstream Firmware Flow.
        # To-Do For a time being hard code the traffic class value.
        # Need to know how to get the traffic class info from flows.
        gem_port = self.get_gem_port_info(v_enet, traffic_class=2)
        if gem_port is None:
            self.log.info('Failed-to-get-gemport')
            return
        v_ont_ani = self.get_v_ont_ani(v_enet.v_enet.data.v_ontani_ref)
        if v_ont_ani is None:
            self.log.info('Failed-to-get-v_ont_ani',
                          v_ont_ani=v_enet.v_enet.data.v_ontani_ref)
            return
        tcont = self.get_tcont_info(v_ont_ani, name=gem_port.tcont_ref)
        if tcont is None:
            self.log.info('Failed-to-get-tcont-info',
                          tcont=gem_port.tcont_ref)
            return
        onu_device = self.adapter_agent.get_child_device(
            self.device_id, onu_id=v_ont_ani.v_ont_ani.data.onu_id)
        if onu_device is None:
            self.log.info('Failed-to-get-onu-device',
                          onu_id=v_ont_ani.v_ont_ani.data.onu_id)
            return

        flow_id = self.get_flow_id(onu_device.proxy_address.onu_id,
                                   onu_device.proxy_address.channel_id,
                                   hsia_id)
        try:
            is_down_stream = False
            self.log.info('Adding-HSIA-upstream-flow',
                          classifier=uplink_classifier,
                          action=uplink_action,
                          gem_port=gem_port,
                          flow_id=flow_id,
                          sched_info=tcont.alloc_id)
            self.bal.add_flow(onu_device.proxy_address.onu_id,
                              onu_device.proxy_address.channel_id,
                              flow_id, gem_port.gemport_id,
                              uplink_classifier, is_down_stream,
                              action_info=uplink_action,
                              sched_id=tcont.alloc_id)
            # To-Do. While addition of one flow is in progress,
            # we cannot add an another flow. Right now use sleep
            # of 5 sec, assuming that addtion of flow is successful.
            time.sleep(10)
        except Exception as e:
            self.log.exception('failed-to-install-HSIA-upstream-flow', e=e,
                               classifier=uplink_classifier,
                               action=uplink_action,
                               onu_id=onu_device.proxy_address.onu_id,
                               intf_id=onu_device.proxy_address.channel_id)
        is_down_stream = True
        # To-Do: For Now hard code the p-bit values.
        downlink_classifier['vlan_pcp'] = 0
        downlink_flow_id = self.get_flow_id(onu_device.proxy_address.onu_id,
                                            onu_device.proxy_address.channel_id,
                                            downlink_hsia_id)
        try:
            self.log.info('Adding-HSIA-downstream-flow',
                          classifier=downlink_classifier,
                          action=downlink_action,
                          gem_port=gem_port,
                          flow_id=downlink_flow_id)
            self.bal.add_flow(onu_device.proxy_address.onu_id,
                              onu_device.proxy_address.channel_id,
                              downlink_flow_id, gem_port.gemport_id,
                              downlink_classifier, is_down_stream,
                              action_info=downlink_action)
            # To-Do. While addition of one flow is in progress,
            # we cannot add an another flow. Right now use sleep
            # of 5 sec, assuming that addtion of flow is successful.
            time.sleep(10)
        except Exception as e:
            self.log.exception('failed-to-install-HSIA-downstream-flow', e=e,
                               classifier=downlink_classifier,
                               action=downlink_action,
                               onu_id=onu_device.proxy_address.onu_id,
                               intf_id=onu_device.proxy_address.channel_id)
