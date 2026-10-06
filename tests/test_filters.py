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
print("ALL FILTER TESTS PASSED")
