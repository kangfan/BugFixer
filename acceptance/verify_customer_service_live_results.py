"""Check saved real CSC fetches and downloaded bytes; no credentials or HTTP calls."""
import hashlib
import json
from datetime import datetime
from pathlib import Path

LIVE = Path(__file__).resolve().parent / "live"
EXPECTED = {
    "2223144": ("domestic_minigame", "H5_WX", [
        ("text", 1633909, "077498346789490496e525765dc0bf1aaab5fd84e1dde14dd24980dd599ec355"),
        ("mp4", 1925998, "b76c1cdfdafb413a2451d6be4a3d114cdda8ce1c41116e09283adaa9ec755447"),
    ]),
    "319220": ("overseas", "GP_JP", [
        ("zip", 124800, "b649335472d736ff2a0c502a66ace804272b0f77bb26a5c45dc048b468ae319f"),
        ("zip", 114264, "10ecc267232f25d33ac2ab7b70a448931b9572676d0d6367cec3a977b67e6c36"),
    ]),
}
checks = []
accounts = [("customer_service", "customer-service"),
            ("customer_service_dragongu", "customer-service-dragongu")]
cases = [(ref, prefix, ticket_id, config) for ref, prefix in accounts
         for ticket_id, config in EXPECTED.items()]
for ref, prefix, ticket_id, (region, channel, expected_files) in cases:
    result = json.loads((LIVE / f"{prefix}-{ticket_id}-result.json").read_text(encoding="utf-8-sig"))
    context = json.loads(Path(result["context_path"]).read_text(encoding="utf-8-sig"))
    assert result["status"] == "ok" and result["read_only"] is True
    assert not result["missing"]
    assert context["id"] == ticket_id and str(context["ticket"]["id"]) == ticket_id
    assert context["region"] == region and context["channel"] == channel
    assert context["description_text"].strip() and context["app_version"]
    assert context["history"]["simpleContents"]
    assert len(result["resources"]) == len(expected_files)
    member_count = 0
    for resource, (kind, size, sha) in zip(result["resources"], expected_files):
        assert resource["status"] == "downloaded" and resource["detected_type"] == kind
        data = Path(resource["path"]).read_bytes()
        assert len(data) == resource["size_bytes"] == size
        assert hashlib.sha256(data).hexdigest() == resource["sha256"] == sha
        if kind == "text":
            assert Path(resource["index"]["path"]).is_file()
        if kind == "zip":
            assert resource["members"]
            for member in resource["members"]:
                data = Path(member["path"]).read_bytes()
                assert len(data) == member["size_bytes"]
                assert hashlib.sha256(data).hexdigest() == member["sha256"]
                if member.get("index"):
                    assert Path(member["index"]["path"]).is_file()
            member_count += len(resource["members"])
    checks.append({"credential_ref": ref, "ticket_id": ticket_id, "region": region, "channel": channel,
                   "status": "passed", "fetched_at": result["fetched_at"],
                   "context_path": result["context_path"], "manifest_path": result["manifest_path"],
                   "resources_verified": len(expected_files), "zip_members_verified": member_count,
                   "description_version_history_present": True, "warnings": result["warnings"]})
report = {"verified_at": datetime.now().astimezone().isoformat(), "credential_refs": [a[0] for a in accounts],
          "scope": "Saved authenticated production fetches; no network calls by verifier.",
          "read_only": True, "business_writes": False, "verification_codes_sent": False, "checks": checks}
path = LIVE / "customer-service-authenticated-acceptance.json"
path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps({"status": "passed", "account_ticket_checks": len(checks), "report": str(path)}))
