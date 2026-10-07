import sys, json
import os; sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "filter_plugins"))
import mtv

def disk(label, gb, ds, mode="persistent", rdm=False):
    b = {"_vimtype": "vim.vm.device.VirtualDisk.FlatVer2BackingInfo", "fileName": "[%s] x/x.vmdk" % ds, "diskMode": mode}
    if rdm: b = {"_vimtype": "vim.vm.device.VirtualDisk.RawDiskMappingVer1BackingInfo", "fileName": "[%s] x/r.vmdk" % ds, "compatibilityMode": "physicalMode", "diskMode": mode}
    return {"_vimtype": "vim.vm.device.VirtualDisk", "capacityInBytes": gb * 1024**3, "deviceInfo": {"label": label}, "backing": b}
def nic(net, mac): return {"_vimtype": "vim.vm.device.VirtualVmxnet3", "macAddress": mac, "deviceInfo": {"label": "Network adapter 1"},
                           "backing": {"_vimtype": "vim.vm.device.VirtualEthernetCard.NetworkBackingInfo", "deviceName": net}}
TPM = {"_vimtype": "vim.vm.device.VirtualTPM", "deviceInfo": {"label": "Virtual TPM"}}
PVSCSI = {"_vimtype": "vim.vm.device.ParaVirtualSCSIController", "deviceInfo": {"label": "SCSI controller 0"}}

def vm(name, moid, gid, full, devs, cbt=True, fw="efi", sb=True, snaps=0, ips=("10.20.0.31",)):
    summary = {"item": name, "failed": False, "instance": {"moid": moid, "hw_power_status": "poweredOn",
               "hw_guest_id": gid, "hw_guest_full_name": full, "instance_uuid": "u-" + moid, "guest_tools_status": "guestToolsRunning"}}
    snap = {"rootSnapshotList": [{"name": "s1", "childSnapshotList": [{"name": "s2", "childSnapshotList": []}]}]} if snaps else None
    detail = {"item": name, "failed": False, "instance": {
        "config": {"guestId": gid, "guestFullName": full, "firmware": fw, "bootOptions": {"efiSecureBootEnabled": sb},
                   "changeTrackingEnabled": cbt, "hardware": {"device": devs + [PVSCSI]}},
        "snapshot": snap, "runtime": {"powerState": "poweredOn"}, "guest": {"toolsRunningStatus": "guestToolsRunning",
        "net": [{"ipAddress": list(ips) + ["fe80::1"]}]}}}
    return summary, detail

cases = [
    vm("win2022-app01", "vm-101", "windows2019srvNext_64Guest", "Microsoft Windows Server 2022 (64-bit)",
       [disk("Hard disk 1", 120, "ds-nfs-01"), disk("Hard disk 2", 200, "ds-nfs-01"), nic("VM Network", "00:50:56:aa:01:01"), TPM]),
    vm("rhel9-web01", "vm-102", "rhel9_64Guest", "Red Hat Enterprise Linux 9 (64-bit)",
       [disk("Hard disk 1", 40, "ds-local-02"), nic("VM Network", "00:50:56:aa:01:02")], sb=False, ips=("10.20.0.41",)),
    vm("win2012-old", "vm-103", "windows8Server64Guest", "Microsoft Windows Server 2012 (64-bit)",
       [disk("Hard disk 1", 80, "ds-nfs-01"), nic("VM Network", "00:50:56:aa:01:03")]),
    vm("sql-big", "vm-104", "windows2019srvNext_64Guest", "Microsoft Windows Server 2025 (64-bit)",
       [disk("Hard disk 1", 100, "ds-fc-01"), disk("Hard disk 2", 2048, "ds-fc-01"), nic("DB VLAN", "00:50:56:aa:01:04")], snaps=2),
    vm("rhel8-rdm", "vm-105", "rhel8_64Guest", "Red Hat Enterprise Linux 8 (64-bit)",
       [disk("Hard disk 1", 40, "ds-fc-01"), disk("Hard disk 2", 500, "ds-fc-01", rdm=True), nic("VM Network", "00:50:56:aa:01:05")]),
    vm("rhel9-nocbt", "vm-106", "rhel9_64Guest", "Red Hat Enterprise Linux 9 (64-bit)",
       [disk("Hard disk 1", 300, "ds-nfs-01"), nic("Unmapped Net", "00:50:56:aa:01:06")], cbt=False),
]
facts = [mtv.mtv_vm_facts(s, d, s["item"]) for s, d in cases]
facts.append(mtv.mtv_vm_facts({"item": "ghost", "failed": True, "msg": "Unable to gather information for non-existing VM ghost"}, {"failed": True}, "ghost"))

f0 = facts[0]
assert f0["os"] == "windows" and f0["has_vtpm"] and f0["secure_boot"] and f0["cbt"] and f0["total_gb"] == 320.0, f0
assert f0["datastores"] == ["ds-nfs-01"] and f0["networks"] == ["VM Network"] and f0["ips"] == ["10.20.0.31"]
assert facts[1]["rhel_major"] == 9 and facts[1]["os"] == "linux"
assert facts[2]["win2012"]
assert facts[3]["snapshots"] == 2 and facts[3]["max_disk_gb"] == 2048.0
assert facts[4]["disks"][1]["rdm"]

maps = [{"source": "VM Network", "type": "multus", "name": "vlan20"}, {"source": "DB VLAN", "type": "pod"}]
out = mtv.mtv_decide(facts, mode="auto", warm_min_disk_gb=100, large_disk_gb=1024, max_vms_per_plan=10, run_id="4711", network_mappings=maps)
d = {x["vm"]: x for x in out["decisions"]}
for x in out["decisions"]: print(mtv.mtv_decision_row(x))
print(json.dumps(out["plans"], indent=1))
assert d["win2022-app01"]["mode"] == "warm"
assert d["rhel9-web01"]["mode"] == "cold"
assert not d["win2012-old"]["eligible"]
assert d["sql-big"]["mode"] == "warm" and d["sql-big"]["plan"] != d["win2022-app01"]["plan"], "big disk gets its own plan"
assert not d["rhel8-rdm"]["eligible"]
assert not d["rhel9-nocbt"]["eligible"] and any("Unmapped Net" in b for b in d["rhel9-nocbt"]["blockers"])
assert not d["ghost"]["eligible"]
# warm requested + CBT off falls back to cold
facts[5]["networks"] = ["VM Network"]
o2 = mtv.mtv_decide([facts[5]], mode="warm", network_mappings=maps, run_id="1")
assert o2["decisions"][0]["mode"] == "cold" and "fell back" in o2["decisions"][0]["reasons"][0]
o3 = mtv.mtv_decide([facts[5]], mode="warm", network_mappings=maps, run_id="1", cold_fallback=False)
assert not o3["decisions"][0]["eligible"]
# chunking
many = [dict(facts[1], vm="r%02d" % i) for i in range(23)]
o4 = mtv.mtv_decide(many, mode="cold", network_mappings=maps, run_id="9", max_vms_per_plan=10)
assert [len(p["vms"]) for p in o4["plans"]] == [10, 10, 3]
# misc
assert mtv.mtv_dns("Win11 Desk_01!!") == "win11-desk-01"
assert mtv.mtv_pick({"a": 1}, ["a", "b"]) == {"a": 1, "b": None}
assert mtv.mtv_result([{"status": "pass"}, {"status": "info"}]) == "pass"
assert mtv.mtv_result([{"status": "warn"}, {"status": "fail"}]) == "fail"
assert mtv.mtv_lsblk_luks('{"blockdevices":[{"name":"/dev/vda","fstype":null,"children":[{"name":"/dev/vda2","fstype":"crypto_LUKS"}]}]}') == ["/dev/vda2"]
assert mtv.mtv_clevis_tpm2({"rc": 0, "item": "/dev/vda2", "stdout_lines": ["1: tpm2 '{\"hash\":\"sha256\"}'"]}) == {"device": "/dev/vda2", "slots": ["1"]}
assert mtv.mtv_nm_profile("u1|System eth0|ens192|")["ifname"] == "ens192"
plan_cr = {"metadata": {"name": "p1"}, "spec": {"warm": True, "vms": [{"id": "vm-101", "name": "win2022-app01"}]},
           "status": {"migration": {"vms": [{"id": "vm-101", "name": "win2022-app01", "phase": "Completed",
               "conditions": [{"type": "Succeeded", "status": "True"}], "pipeline": [{"name": "DiskTransfer", "phase": "Completed"}],
               "warm": {"successes": 3, "failures": 0}}]}}}
assert mtv.mtv_planned_vms([plan_cr]) == [{"vm": "win2022-app01", "moid": "vm-101", "plan": "p1", "warm": True}]
oc = mtv.mtv_plan_vm_outcomes([plan_cr]); assert oc[0]["ok"] and oc[0]["checks"][2]["detail"] == "3 ok, 0 failed"
assert mtv.mtv_ref({"vm": "a", "moid": "vm-1"}) == {"name": "a", "id": "vm-1"}

# Distributed port groups: resolved by name, referenced by id in the NetworkMap
def dvnic(key, mac): return {"_vimtype": "vim.vm.device.VirtualVmxnet3", "macAddress": mac, "deviceInfo": {"label": "Network adapter 1"},
    "backing": {"_vimtype": "vim.vm.device.VirtualEthernetCard.DistributedVirtualPortBackingInfo", "port": {"portgroupKey": key, "switchUuid": "50 2a"}}}
pgs = mtv.mtv_portgroups({"dvs_portgroup_info": {"dvs1": [{"key": "dvportgroup-210", "portgroup_name": "segment-migrating-to-ocpvirt"}]}})
assert pgs == {"dvportgroup-210": "segment-migrating-to-ocpvirt"}
assert mtv.mtv_portgroups({"failed": True, "msg": "no permission"}) == {}
sd, dd = vm("haproxy", "vm-201", "rhel9_64Guest", "Red Hat Enterprise Linux 9 (64-bit)", [disk("Hard disk 1", 20, "ds1"), dvnic("dvportgroup-210", "00:50:56:aa:02:01")])
fd = mtv.mtv_vm_facts(sd, dd, "haproxy", pgs)
assert fd["networks"] == ["segment-migrating-to-ocpvirt"] and fd["network_ids"] == {"segment-migrating-to-ocpvirt": "dvportgroup-210"}, fd
by_name = [{"source": "segment-migrating-to-ocpvirt", "type": "pod"}]
assert mtv.mtv_decide([fd], network_mappings=by_name)["decisions"][0]["eligible"]
assert mtv.mtv_net_entries(by_name, [fd, fd], "migrated-vms") == [{"source": {"id": "dvportgroup-210"}, "destination": {"type": "pod"}}]
# lookup failed: the raw key still works, in either spelling
fr = mtv.mtv_vm_facts(sd, dd, "haproxy")
assert fr["networks"] == ["dvportgroup:dvportgroup-210"]
assert not mtv.mtv_decide([fr], network_mappings=by_name)["decisions"][0]["eligible"]
for src in ("dvportgroup-210", "dvportgroup:dvportgroup-210"):
    mp = [{"source": src, "type": "multus", "name": "vlan210"}]
    assert mtv.mtv_decide([fr], network_mappings=mp)["decisions"][0]["eligible"]
    assert mtv.mtv_net_entries(mp, [fr], "migrated-vms") == [{"source": {"id": "dvportgroup-210"}, "destination": {"type": "multus", "name": "vlan210", "namespace": "migrated-vms"}}]
# standard port groups are unchanged: by name
assert mtv.mtv_net_entries(maps, [facts[0], facts[3]], "migrated-vms") == [
    {"source": {"name": "VM Network"}, "destination": {"type": "multus", "name": "vlan20", "namespace": "migrated-vms"}},
    {"source": {"name": "DB VLAN"}, "destination": {"type": "pod"}}]
# discovery stored before this change has no network_ids
old_f = dict(facts[0]); old_f.pop("network_ids")
assert mtv.mtv_decide([old_f], network_mappings=maps)["decisions"][0]["eligible"]
# Fixed plan name
on = mtv.mtv_decide([fd], mode="cold", run_id="demo1", network_mappings=by_name, plan_name="move-webapp-vmware")
assert [p["name"] for p in on["plans"]] == ["move-webapp-vmware"] and on["decisions"][0]["plan"] == "move-webapp-vmware"
om = mtv.mtv_decide(facts, mode="auto", run_id="4711", network_mappings=maps, plan_name="move-webapp-vmware")
assert [p["name"] for p in om["plans"]][:2] == ["move-webapp-vmware", "move-webapp-vmware-02"], om["plans"]
# Storage mapping sources
SC = "ocs-external-storagecluster-ceph-rbd"
assert f0["datastore_ids"] == [] and f0["datastores"] == ["ds-nfs-01"]
assert mtv.mtv_storage_entries([facts[0], facts[3]], SC) == [
    {"source": {"name": "ds-nfs-01"}, "destination": {"storageClass": SC}},
    {"source": {"name": "ds-fc-01"}, "destination": {"storageClass": SC}}]
# vCenter hides the file path: fall back to the datastore id on the disk
hid = disk("Hard disk 1", 5, "x"); hid["backing"]["fileName"] = ""; hid["backing"]["datastore"] = "vim.Datastore:datastore-42"
sh, dh = vm("database-user1", "vm-301", "centos8_64Guest", "centos8_64Guest", [hid, nic("VM Network", "00:50:56:aa:03:01")])
fh = mtv.mtv_vm_facts(sh, dh, "database-user1")
assert fh["datastores"] == [] and fh["datastore_ids"] == ["datastore-42"], fh
assert mtv.mtv_storage_entries([fh], SC) == [{"source": {"id": "datastore-42"}, "destination": {"storageClass": SC}}]
assert "datastore-42" in [c for c in mtv.mtv_discover_checks(fh) if c["id"] == "disks"][0]["detail"]
# ... or to the VM-level names
dh2 = json.loads(json.dumps(dh)); dh2["instance"]["config"]["hardware"]["device"][0]["backing"]["datastore"] = None
dh2["instance"]["config"]["datastoreUrl"] = [{"name": "workload_share", "url": "ds:///x/"}]
assert mtv.mtv_vm_facts(sh, dh2, "database-user1")["datastores"] == ["workload_share"]
# nothing from vCenter at all: MTV inventory ids, then the explicit override
fn = mtv.mtv_vm_facts(sh, {"failed": True}, "database-user1")
assert fn["datastores"] == [] and fn["datastore_ids"] == [] and mtv.mtv_storage_entries([fn], SC) == []
inv = [{"status": 200, "json": {"id": "vm-301", "disks": [{"file": "[ws] a.vmdk", "datastore": {"kind": "Datastore", "id": "datastore-7"}},
                                                         {"file": "[ws] b.vmdk", "datastore": {"kind": "Datastore", "id": "datastore-7"}}]}},
       {"status": 404, "msg": "not found"}, {"skipped": True}]
assert mtv.mtv_inventory_datastores(inv) == ["datastore-7"]
assert mtv.mtv_inventory_datastores([{"status": 200, "json": [{"id": "datastore-7", "name": "ws"}, {"id": "datastore-9"}]}]) == ["datastore-7", "datastore-9"]
assert mtv.mtv_inventory_datastores([]) == [] and mtv.mtv_inventory_datastores([{"skipped": True}]) == []
assert mtv.mtv_storage_entries([fn], SC, [], ["datastore-7"]) == [{"source": {"id": "datastore-7"}, "destination": {"storageClass": SC}}]
assert mtv.mtv_storage_entries([fh], SC, ["workload_share", "datastore-9"]) == [
    {"source": {"name": "workload_share"}, "destination": {"storageClass": SC}},
    {"source": {"id": "datastore-9"}, "destination": {"storageClass": SC}}]
assert mtv.mtv_storage_entries([fh], SC, "workload_share") == [{"source": {"name": "workload_share"}, "destination": {"storageClass": SC}}]
# Maps built from MTV's inventory
ans = [{"status": 200, "item": {"vm": "winweb01-user1", "moid": "vm-9"},
        "json": {"id": "vm-9", "networks": [{"kind": "Network", "id": "dvportgroup-210"}],
                 "nics": [{"network": {"kind": "Network", "id": "dvportgroup-210"}, "mac": "00:50:56:00:00:01"}],
                 "disks": [{"file": "[ws] a.vmdk", "datastore": {"kind": "Datastore", "id": "datastore-7"}}]}},
       {"status": 404, "item": {"vm": "gone"}, "json": {}}, {"skipped": True, "item": {"vm": "skipped"}}]
iv = mtv.mtv_inventory_vms(ans)
assert iv == {"winweb01-user1": {"networks": ["dvportgroup-210"], "datastores": ["datastore-7"]}}, iv
nn = mtv.mtv_inventory_names({"status": 200, "json": [{"id": "dvportgroup-210", "name": "segment-migrating-to-ocpvirt"}, {"id": "network-1", "name": "VM Network"}]})
assert nn["dvportgroup-210"] == "segment-migrating-to-ocpvirt" and mtv.mtv_inventory_names({"skipped": True}) == {}
blank = {"vm": "winweb01-user1", "moid": "vm-9", "networks": [], "datastores": []}   # vCenter said nothing
mp = mtv.mtv_maps([blank], by_name, SC, inventory=iv, net_names=nn, default_namespace="migrated-vms")
assert mp["network"] == [{"source": {"id": "dvportgroup-210"}, "destination": {"type": "pod"}}], mp
assert mp["storage"] == [{"source": {"id": "datastore-7"}, "destination": {"storageClass": SC}}] and mp["unmapped"] == []
assert mp["origin"] == {"winweb01-user1": "MTV inventory"}
# the name is unknown to MTV or differs: reported, not silently dropped
mu = mtv.mtv_maps([blank], by_name, SC, inventory=iv, net_names={})
assert mu["network"] == [] and mu["unmapped"] == ["winweb01-user1: dvportgroup-210"]
mu2 = mtv.mtv_maps([blank], [{"source": "other", "type": "pod"}], SC, inventory=iv, net_names=nn)
assert mu2["unmapped"] == ["winweb01-user1: segment-migrating-to-ocpvirt (dvportgroup-210)"]
# catch-all, and mapping by id
for src in ("*", "dvportgroup-210"):
    assert mtv.mtv_maps([blank], [{"source": src, "type": "pod"}], SC, inventory=iv)["network"] == mp["network"]
assert mtv.mtv_decide([fr], network_mappings=[{"source": "*", "type": "pod"}])["decisions"][0]["eligible"]
# no inventory answer: vCenter facts as before; mixed runs combine both
mv = mtv.mtv_maps([fd, facts[0]], by_name + maps, SC, inventory={}, default_namespace="migrated-vms")
assert mv["network"] == [{"source": {"id": "dvportgroup-210"}, "destination": {"type": "pod"}},
                         {"source": {"name": "VM Network"}, "destination": {"type": "multus", "name": "vlan20", "namespace": "migrated-vms"}}], mv
assert {"source": {"name": "ds-nfs-01"}, "destination": {"storageClass": SC}} in mv["storage"] and mv["origin"]["haproxy"] == "vCenter"
mx = mtv.mtv_maps([blank, facts[0]], by_name + maps, SC, inventory=iv, net_names=nn, default_namespace="migrated-vms")
assert [e["source"] for e in mx["network"]] == [{"id": "dvportgroup-210"}, {"name": "VM Network"}]
assert [e["source"] for e in mx["storage"]] == [{"name": "ds-nfs-01"}, {"id": "datastore-7"}], mx["storage"]
# nothing known anywhere: every datastore of the provider, then the override
assert mtv.mtv_maps([blank], by_name, SC, inventory={}, all_datastores=["datastore-7", "datastore-9"])["storage"] == [
    {"source": {"id": "datastore-7"}, "destination": {"storageClass": SC}}, {"source": {"id": "datastore-9"}, "destination": {"storageClass": SC}}]
assert mtv.mtv_maps([blank], by_name, SC, inventory=iv, source_datastores=["ws"])["storage"] == [{"source": {"name": "ws"}, "destination": {"storageClass": SC}}]
assert mtv.mtv_maps([blank], by_name, SC)["storage"] == []
# VM networks unknown everywhere: every mapping given goes into the map
mk = mtv.mtv_maps([blank], by_name, SC, inventory={}, default_namespace="migrated-vms")
assert mk["network"] == [{"source": {"name": "segment-migrating-to-ocpvirt"}, "destination": {"type": "pod"}}] and mk["networks_unknown"] == ["winweb01-user1"], mk
mk2 = mtv.mtv_maps([blank], [{"source": "dvportgroup:dvportgroup-210", "type": "pod"}, {"source": "*", "type": "pod"}, {"source": "network-5", "type": "multus", "name": "n"}], SC, default_namespace="ns")
assert [e["source"] for e in mk2["network"]] == [{"id": "dvportgroup-210"}, {"id": "network-5"}], mk2
assert mtv.mtv_maps([blank], [], SC)["network"] == [] and mtv.mtv_maps([blank], [{"source": "*", "type": "pod"}], SC)["network"] == []
assert mv["networks_unknown"] == [] and mp["networks_unknown"] == []
# Lab post-migration helpers
lp = {"metadata": {"name": "aap-demo-812"}, "spec": {"targetNamespace": "aap-demo-812", "vms": [{"name": "database-user1"}, {"id": "vm-102", "name": "winweb01-user1"}, {"name": "never-ran"}]},
      "status": {"migration": {"vms": [
          {"id": "vm-101", "name": "database-user1", "phase": "Completed", "conditions": [{"type": "Succeeded", "status": "True"}],
           "pipeline": [{"name": "DiskTransfer", "phase": "Completed"}], "started": "t0", "completed": "t1"},
          {"id": "vm-102", "phase": "Completed", "error": {"reasons": ["disk copy failed"]}, "conditions": [{"type": "Failed", "status": "True"}], "pipeline": []}]}}}
lo = mtv.mtv_lab_outcomes(lp)
assert [(o["vm"], o["ok"], o["target_name"]) for o in lo] == [("database-user1", True, "database-user1"), ("winweb01-user1", False, "winweb01-user1"), ("never-ran", False, "never-ran")], lo
assert mtv.mtv_lab_outcomes({}) == [] and mtv.mtv_lab_outcomes(None) == []
kvms = [{"metadata": {"name": "other", "namespace": "aap-demo-812"}}, {"metadata": {"name": "db-renamed", "namespace": "aap-demo-812", "labels": {"vmID": "vm-101"}}}]
lm = mtv.mtv_lab_match(lo, kvms)
assert lm[0]["target"] == {"name": "db-renamed", "namespace": "aap-demo-812", "uses_running": False} and lm[1]["target"] is None
assert mtv.mtv_lab_match(lo, [{"metadata": {"name": "database-user1"}, "spec": {"running": False}}])[0]["target"]["uses_running"] is True
assert mtv.mtv_lab_match(lo, [{"metadata": {"name": "database-user1", "namespace": "n"}}])[0]["target"]["name"] == "database-user1"
vmi = {"resources": [{"status": {"phase": "Running", "nodeName": "worker-1", "interfaces": [{"ipAddress": "10.128.2.9", "ipAddresses": ["10.128.2.9", "fe80::1"]}],
                                 "conditions": [{"type": "AgentConnected", "status": "True"}]}}]}
lt = mtv.mtv_lab_target(lm[0], vmi)
assert mtv.mtv_result(lt["checks"]) == "pass" and lt["ips"] == ["10.128.2.9"] and lt["phase"] == "Running", lt
assert mtv.mtv_result(mtv.mtv_lab_target(lm[0], {"resources": []})["checks"]) == "warn"
assert mtv.mtv_result(mtv.mtv_lab_target(dict(lm[0], target=None), None)["checks"]) == "fail"
assert mtv.mtv_result(mtv.mtv_lab_target(lm[1], None)["checks"]) == "pass"      # not checked: info only
assert mtv.mtv_lab_pair_line((lm[0], lt)) == mtv.mtv_lab_line(lm[0], lt)
assert mtv.mtv_lab_line(lm[0], lt) == "database-user1: migrated -> aap-demo-812/db-renamed, Running, IP 10.128.2.9"
assert "NOT migrated" in mtv.mtv_lab_line(lm[1], mtv.mtv_lab_target(lm[1], None)) and "disk copy failed" in mtv.mtv_lab_line(lm[1], {})
print("ALL FILTER TESTS PASSED")
