"""Filters for the AAP + MTV standard-path migration workflow.

All migration decisions live here so they can be unit tested and read in one
place, instead of being spread through long Jinja expressions.
"""
import re
from datetime import datetime, timezone

GIB = 1024 ** 3
RESULT_RANK = {"pass": 0, "info": 0, "skip": 0, "warn": 1, "fail": 2}
VM_STAGES = ["discover", "decide", "readiness", "plan", "cutover_prep",
             "migrate", "target", "postcheck", "rollback"]
PRE_STAGES = ["discover", "decide", "readiness"]
PLATFORM_STAGES = ["tune", "restore"]


def mtv_dns(value, maxlen=63):
    """DNS-1123 label: lowercase alphanumerics and '-', trimmed to maxlen."""
    s = re.sub(r"[^a-z0-9-]+", "-", str(value).lower()).strip("-")
    s = s[: int(maxlen)].strip("-")
    return s or "x"


def _get(obj, path, default=None):
    if obj is None:
        return default
    if isinstance(obj, dict) and path in obj:
        return obj[path]
    head, _, rest = path.partition(".")
    if isinstance(obj, dict) and head in obj:
        return obj[head] if not rest else _get(obj[head], rest, default)
    return default


def _count_snapshots(nodes):
    total = 0
    for n in nodes or []:
        total += 1 + _count_snapshots(n.get("childSnapshotList"))
    return total


def _classify_os(guest_id, full_name):
    gid, full = (guest_id or "").lower(), (full_name or "").lower()
    blob = gid + " " + full
    if "windows" in blob:
        return "windows"
    for key in ("rhel", "red hat", "centos", "rocky", "alma", "oracle", "sles",
                "suse", "ubuntu", "debian", "fedora", "linux"):
        if key in blob:
            return "linux"
    return "unknown"


def mtv_vm_facts(summary_result, detail_result, name):
    """Normalise two vmware_guest_info results (summary + vsphere schema)."""
    facts = {"vm": name, "found": False}
    if not summary_result or summary_result.get("failed") or "instance" not in summary_result:
        facts["error"] = (summary_result or {}).get("msg", "VM not found in vCenter")
        return facts
    s = summary_result["instance"]
    d = (detail_result or {}).get("instance", {}) if not (detail_result or {}).get("failed") else {}

    guest_id = _get(d, "config.guestId") or s.get("hw_guest_id")
    full_name = _get(d, "config.guestFullName") or s.get("hw_guest_full_name")
    disks, nics = [], []
    has_vtpm = has_pvscsi = False
    for dev in _get(d, "config.hardware.device", []) or []:
        vt = str(dev.get("_vimtype", ""))
        label = str(_get(dev, "deviceInfo.label", "") or "")
        backing = dev.get("backing") or {}
        if "capacityInBytes" in dev or vt.endswith("VirtualDisk"):
            fname = backing.get("fileName", "") or ""
            m = re.match(r"^\[(.+?)\]", fname)
            disks.append({
                "label": label,
                "gb": round((dev.get("capacityInBytes") or 0) / GIB, 1),
                "datastore": m.group(1) if m else "",
                "mode": backing.get("diskMode", ""),
                "rdm": "compatibilityMode" in backing or "RawDiskMapping" in str(backing.get("_vimtype", "")),
                "sharing": backing.get("sharing", ""),
            })
        elif "macAddress" in dev:
            net = backing.get("deviceName")
            if not net and isinstance(backing.get("port"), dict):
                net = "dvportgroup:" + str(backing["port"].get("portgroupKey", ""))
            if not net and backing.get("opaqueNetworkId"):
                net = "opaque:" + str(backing["opaqueNetworkId"])
            nics.append({"label": label, "mac": dev.get("macAddress", ""),
                         "network": net or "", "type": vt.split(".")[-1] or label})
        elif "VirtualTPM" in vt or label.lower().startswith("virtual tpm"):
            has_vtpm = True
        elif "ParaVirtualSCSIController" in vt:
            has_pvscsi = True

    ips = []
    for n in _get(d, "guest.net", []) or []:
        ips.extend([ip for ip in (n.get("ipAddress") or []) if ":" not in ip])
    if not ips and s.get("ipv4"):
        ips = [s["ipv4"]]

    os_family = _classify_os(guest_id, full_name)
    m = re.search(r"(rhel|red hat enterprise linux)\D*(\d+)", ((guest_id or "") + " " + (full_name or "")).lower())
    facts.update({
        "found": True,
        "moid": s.get("moid", ""),
        "uuid": s.get("instance_uuid", ""),
        "power": _get(d, "runtime.powerState") or s.get("hw_power_status", ""),
        "tools": _get(d, "guest.toolsRunningStatus") or s.get("guest_tools_status", ""),
        "guest_id": guest_id or "",
        "guest_full_name": full_name or "",
        "os": os_family,
        "rhel_major": int(m.group(2)) if m else None,
        "win2012": os_family == "windows" and (str(guest_id).lower().startswith("windows8server") or "2012" in str(full_name)),
        "firmware": _get(d, "config.firmware", "") or "",
        "secure_boot": bool(_get(d, "config.bootOptions.efiSecureBootEnabled", False)),
        "has_vtpm": has_vtpm,
        "has_pvscsi": has_pvscsi,
        "cbt": bool(_get(d, "config.changeTrackingEnabled", False)),
        "snapshots": _count_snapshots(_get(d, "snapshot.rootSnapshotList", [])),
        "disks": disks,
        "nics": nics,
        "ips": sorted(set(ips)),
        "total_gb": round(sum(x["gb"] for x in disks), 1),
        "max_disk_gb": max([x["gb"] for x in disks] or [0]),
        "datastores": sorted({x["datastore"] for x in disks if x["datastore"]}),
        "networks": sorted({x["network"] for x in nics if x["network"]}),
    })
    return facts


def _check(cid, title, status, detail=""):
    return {"id": cid, "title": title, "status": status, "detail": str(detail)}


def mtv_discover_checks(f):
    """Source-side checks for one VM, from mtv_vm_facts output."""
    if not f.get("found"):
        return [_check("found", "VM found in vCenter", "fail", f.get("error", "not found"))]
    c = [_check("found", "VM found in vCenter", "pass", "%s (%s)" % (f["moid"], f["power"]))]
    if f["os"] == "unknown":
        c.append(_check("os", "Guest OS recognised", "fail", f["guest_full_name"] or f["guest_id"]))
    elif f["win2012"]:
        c.append(_check("os", "Guest OS supported", "fail", "Windows Server 2012 / 2012 R2 is blocked"))
    else:
        c.append(_check("os", "Guest OS supported", "pass", f["guest_full_name"]))
    c.append(_check("firmware", "Firmware and Secure Boot recorded", "info",
                    "%s, Secure Boot %s" % (f["firmware"] or "bios", "on" if f["secure_boot"] else "off")))
    c.append(_check("vtpm", "vTPM recorded", "warn" if f["has_vtpm"] else "info",
                    "source has a vTPM: its state will not move" if f["has_vtpm"] else "no vTPM on source"))
    c.append(_check("cbt", "Changed Block Tracking", "pass" if f["cbt"] else "warn",
                    "enabled" if f["cbt"] else "disabled: warm migration not possible"))
    c.append(_check("snapshots", "Snapshot chain", "warn" if f["snapshots"] else "pass",
                    "%d snapshot(s): consolidate before migrating" % f["snapshots"] if f["snapshots"] else "none"))
    indep = [x["label"] for x in f["disks"] if "independent" in (x["mode"] or "")]
    rdm = [x["label"] for x in f["disks"] if x["rdm"]]
    shared = [x["label"] for x in f["disks"] if x["sharing"] == "sharingMultiWriter"]
    c.append(_check("independent_disks", "No independent disks", "fail" if indep else "pass", ", ".join(indep) or "none"))
    c.append(_check("rdm", "No RDM disks", "fail" if rdm else "pass", ", ".join(rdm) or "none"))
    if shared:
        c.append(_check("shared_disks", "Multi-writer disks", "warn", ", ".join(shared) + ": migrate with care"))
    tools_ok = str(f["tools"]).lower() in ("guesttoolsrunning", "running")
    c.append(_check("tools", "VMware Tools running", "pass" if tools_ok else "warn",
                    f["tools"] or "unknown") )
    c.append(_check("disks", "Disks", "info", "; ".join(
        "%s %sGB on %s" % (x["label"], x["gb"], x["datastore"]) for x in f["disks"]) or "none"))
    c.append(_check("nics", "NICs", "info", "; ".join(
        "%s %s on %s" % (x["type"], x["mac"], x["network"]) for x in f["nics"]) or "none"))
    return c


def mtv_decide(discovery, mode="auto", warm_min_disk_gb=100, large_disk_gb=1024,
               max_vms_per_plan=10, run_id="manual", network_mappings=None,
               cold_fallback=True):
    """Return {'decisions': [...], 'plans': [...]} for a list of VM facts."""
    mapped = {m.get("source") for m in (network_mappings or [])}
    decisions = []
    for f in discovery:
        checks = [x for x in mtv_discover_checks(f) if x["status"] == "fail"]
        blockers = [x["title"] + ": " + x["detail"] for x in checks]
        if f.get("found"):
            for net in f["networks"]:
                if net not in mapped:
                    blockers.append("Network '%s' has no entry in network_mappings" % net)
        reasons = []
        chosen = None
        if not blockers:
            requested = mode if mode in ("warm", "cold") else "auto"
            if requested == "auto":
                if f["total_gb"] >= float(warm_min_disk_gb) and f["cbt"]:
                    chosen, why = "warm", "auto: %sGB >= %sGB and CBT on" % (f["total_gb"], warm_min_disk_gb)
                else:
                    chosen = "cold"
                    why = "auto: CBT off" if not f["cbt"] else "auto: %sGB < %sGB" % (f["total_gb"], warm_min_disk_gb)
                reasons.append(why)
            elif requested == "warm" and not f["cbt"]:
                if cold_fallback:
                    chosen = "cold"
                    reasons.append("warm requested but CBT is off: fell back to cold")
                else:
                    blockers.append("Warm requested but CBT is off")
            else:
                chosen = requested
                reasons.append("%s requested" % requested)
            if chosen == "warm" and f["snapshots"]:
                reasons.append("existing snapshots: consolidate first")
            if f["max_disk_gb"] >= float(large_disk_gb):
                reasons.append("disk >= %sGB: own plan" % large_disk_gb)
        decisions.append({
            "vm": f["vm"], "os": f.get("os", "unknown"),
            "mode": chosen if not blockers else None,
            "eligible": not blockers,
            "blockers": blockers, "reasons": reasons,
            "plan": None, "source": f,
        })

    plans, counter = [], {"warm": 0, "cold": 0}
    def _new_plan(m):
        counter[m] += 1
        p = {"name": mtv_dns("mtv-%s-%s-%02d" % (run_id, m, counter[m]), 63),
             "warm": m == "warm", "vms": []}
        plans.append(p)
        return p
    for m in ("warm", "cold"):
        current = None
        for d in decisions:
            if d["mode"] != m:
                continue
            if d["source"]["max_disk_gb"] >= float(large_disk_gb):
                p = _new_plan(m)
            else:
                if current is None or len(current["vms"]) >= int(max_vms_per_plan):
                    current = _new_plan(m)
                p = current
            p["vms"].append(d["vm"])
            d["plan"] = p["name"]
    for d in decisions:
        d["checks"] = (
            [_check("eligible", "Eligible for migration", "fail" if d["blockers"] else "pass",
                    "; ".join(d["blockers"]) or "yes")]
            + ([_check("mode", "Migration mode", "info", "%s (%s)" % (d["mode"], "; ".join(d["reasons"])))]
               if d["mode"] else [])
            + ([_check("plan", "Plan group", "info", d["plan"])] if d["plan"] else []))
    return {"decisions": decisions, "plans": plans}


def mtv_pick(spec, keys):
    """Return {key: spec[key] or None} so absent keys can be restored as null."""
    spec = spec or {}
    return {k: spec.get(k) for k in keys}


def mtv_worst(statuses):
    worst = "pass"
    for s in statuses or []:
        if RESULT_RANK.get(s, 0) > RESULT_RANK[worst]:
            worst = "fail" if s == "fail" else "warn"
    return worst


def mtv_result(checks):
    return mtv_worst([c.get("status") for c in checks or []])


def mtv_report_model(configmaps, decisions, run_id, phase="final"):
    """Build the report model from evidence ConfigMaps and the decision table."""
    import json
    stages = PRE_STAGES if phase == "pre" else VM_STAGES
    by_vm, platform = {}, {}
    for cm in configmaps or []:
        data = (cm.get("data") or {}).get("evidence.json")
        if not data:
            continue
        ev = json.loads(data)
        if ev.get("vm") == "platform":
            platform[ev["stage"]] = ev
        else:
            by_vm.setdefault(ev["vm"], {})[ev["stage"]] = ev
    vms = []
    for d in decisions or []:
        st = by_vm.get(d["vm"], {})
        shown = {k: st[k] for k in stages if k in st}
        overall = mtv_worst([v["result"] for v in shown.values()]) if shown else "warn"
        vms.append({"vm": d["vm"], "os": d["os"], "mode": d.get("mode") or "-",
                    "plan": d.get("plan") or "-", "stages": shown, "overall": overall,
                    "eligible": d.get("eligible", False)})
    counts = {"total": len(vms)}
    for r in ("pass", "warn", "fail"):
        counts[r] = len([v for v in vms if v["overall"] == r])
    return {"run_id": str(run_id), "phase": phase,
            "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "stages": stages, "platform_stages": PLATFORM_STAGES,
            "vms": vms, "platform": platform, "counts": counts}


def mtv_decision_row(d):
    why = "; ".join(d.get("blockers") or d.get("reasons") or [])
    return "%-26s %-8s %-5s %-24s %s" % (d["vm"][:26], d.get("os", "")[:8], (d.get("mode") or "BLOCK")[:5],
                                         (d.get("plan") or "-")[:24], why)


def mtv_lsblk_luks(lsblk_json):
    """Paths of crypto_LUKS devices from `lsblk -J -p -o NAME,FSTYPE,TYPE`."""
    import json
    data = json.loads(lsblk_json) if isinstance(lsblk_json, str) else lsblk_json
    found = []
    def walk(nodes):
        for n in nodes or []:
            if n.get("fstype") == "crypto_LUKS":
                found.append(n.get("name"))
            walk(n.get("children"))
    walk((data or {}).get("blockdevices"))
    return found


def mtv_nm_profile(line):
    """'uuid|name|ifname|mac' -> dict (output of the readiness nmcli loop)."""
    parts = (str(line).split("|") + ["", "", "", ""])[:4]
    return {"uuid": parts[0], "name": parts[1], "ifname": parts[2], "mac": parts[3]}


def mtv_clevis_tpm2(result):
    """From a looped `clevis luks list -d DEV` result, return TPM2 slots or None."""
    if not isinstance(result, dict) or result.get("rc", 1) != 0:
        return None
    slots = [ln.split(":", 1)[0].strip() for ln in result.get("stdout_lines", []) if "tpm2" in ln]
    return {"device": result.get("item"), "slots": slots} if slots else None


def mtv_report_row(v, stages):
    cells = []
    for st in stages:
        ev = v["stages"].get(st)
        cells.append("%s=%s" % (st, ev["result"].upper() if ev else "-"))
    return "%-24s %-5s %s" % (v["vm"][:24], v["overall"].upper(), " ".join(cells))


def mtv_cm_vms(resources, result=None):
    """VM names (from the mtv.demo/vm-name annotation) of evidence ConfigMaps,
    optionally only those whose mtv.demo/result label matches `result`."""
    names = []
    for r in resources or []:
        meta = r.get("metadata") or {}
        if result and (meta.get("labels") or {}).get("mtv.demo/result") not in (
                [result] if isinstance(result, str) else result):
            continue
        name = (meta.get("annotations") or {}).get("mtv.demo/vm-name")
        if name:
            names.append(name)
    return names


def mtv_plan_filter(plan, allowed):
    """Copy of a planned group keeping only VMs in `allowed`."""
    p = dict(plan)
    p["vms"] = [v for v in plan.get("vms", []) if v in allowed]
    return p


def mtv_planned_vms(plans):
    """Flatten Plan CRs into [{vm, moid, plan, warm}]."""
    out = []
    for p in plans or []:
        spec = p.get("spec") or {}
        warm = bool(spec.get("warm")) or spec.get("type") == "warm"
        for v in spec.get("vms") or []:
            out.append({"vm": v.get("name") or v.get("id"), "moid": v.get("id", ""),
                        "plan": p["metadata"]["name"], "warm": warm})
    return out


def mtv_plan_vm_outcomes(plans):
    """Per-VM checks from Plan.status.migration.vms."""
    out = []
    for p in plans or []:
        pname = p["metadata"]["name"]
        status = (p.get("status") or {}).get("migration") or {}
        for v in status.get("vms") or []:
            conds = {c.get("type"): c.get("status") for c in v.get("conditions") or []}
            ok = conds.get("Succeeded") == "True"
            err = v.get("error") or {}
            steps = ", ".join("%s:%s" % (s.get("name"), s.get("phase")) for s in v.get("pipeline") or [])
            warm = v.get("warm") or {}
            checks = [
                {"id": "migrated", "title": "MTV migration succeeded",
                 "status": "pass" if ok else "fail",
                 "detail": "phase %s%s" % (v.get("phase"), (": " + "; ".join(err.get("reasons") or [])) if err else "")},
                {"id": "pipeline", "title": "Pipeline", "status": "info", "detail": steps or "-"},
            ]
            if warm:
                checks.append({"id": "precopies", "title": "Warm precopies", "status": "info",
                               "detail": "%s ok, %s failed" % (warm.get("successes", 0), warm.get("failures", 0))})
            out.append({"vm": v.get("name") or v.get("id"), "ok": ok, "checks": checks,
                        "facts": {"plan": pname, "id": v.get("id"), "new_name": v.get("newName", ""),
                                  "phase": v.get("phase"), "started": v.get("started"),
                                  "completed": v.get("completed")}})
    return out


def mtv_ref(planned):
    """MTV ref for a planned VM entry, used in Migration.spec.cancel."""
    ref = {"name": planned["vm"]}
    if planned.get("moid"):
        ref["id"] = planned["moid"]
    return ref


def mtv_fmt_check(c):
    return "[%s] %s: %s" % (str(c.get("status", "")).upper(), c.get("title", ""), c.get("detail", ""))


def mtv_version_ge(a, b):
    def parts(v):
        return [int(x) for x in re.findall(r"\d+", str(v))] or [0]
    return parts(a) >= parts(b)


class FilterModule(object):
    def filters(self):
        return {
            "mtv_dns": mtv_dns,
            "mtv_vm_facts": mtv_vm_facts,
            "mtv_discover_checks": mtv_discover_checks,
            "mtv_decide": mtv_decide,
            "mtv_pick": mtv_pick,
            "mtv_result": mtv_result,
            "mtv_worst": mtv_worst,
            "mtv_report_model": mtv_report_model,
            "mtv_version_ge": mtv_version_ge,
            "mtv_fmt_check": mtv_fmt_check,
            "mtv_decision_row": mtv_decision_row,
            "mtv_lsblk_luks": mtv_lsblk_luks,
            "mtv_plan_filter": mtv_plan_filter,
            "mtv_planned_vms": mtv_planned_vms,
            "mtv_plan_vm_outcomes": mtv_plan_vm_outcomes,
            "mtv_cm_vms": mtv_cm_vms,
            "mtv_ref": mtv_ref,
            "mtv_report_row": mtv_report_row,
            "mtv_nm_profile": mtv_nm_profile,
            "mtv_clevis_tpm2": mtv_clevis_tpm2,
        }
