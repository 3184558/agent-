"""全库结构/引用元数据审计；引用存在与医学内容正确分开记录。"""

import csv
import html
import json
import re
import sys
import time
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path

import httpx
from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.database import documents, engine, source_reviews

out = ROOT / "review"
out.mkdir(exist_ok=True)
with engine.connect() as connection:
    rows = [dict(row) for row in connection.execute(select(documents)).mappings()]
# 只向 NLM 发送公开 PMID 获取元数据；不上传健康描述或合成对话。
pmids = sorted(
    {
        str(ref.get("pmid", ""))
        for row in rows
        for ref in row["references"]
        if re.fullmatch(r"\d+", str(ref.get("pmid", "")))
    }
)
cache_path = out / "pubmed_metadata.json"
if cache_path.exists():
    cache = json.loads(cache_path.read_text(encoding="utf-8"))
else:
    cache = {}
errors = []
with httpx.Client(timeout=30) as client:
    missing = []
    for uncached_pmid in pmids:
        if uncached_pmid not in cache:
            missing.append(uncached_pmid)
    for offset in range(0, len(missing), 150):
        batch = missing[offset : offset + 150]
        try:
            response = client.get(
                "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi",
                params={"db": "pubmed", "id": ",".join(batch), "retmode": "json"},
            )
            response.raise_for_status()
            payload = response.json()["result"]
            for pmid in batch:
                cache[pmid] = payload.get(pmid, {"error": "not returned"})
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            errors.append({"batch": offset, "error_type": type(exc).__name__})
        cache_path.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
        print(
            f"PubMed metadata {min(offset+150,len(missing))}/{len(missing)}", flush=True
        )
        time.sleep(0.4)


def normalize(value):
    """将引用标题转成小写字母数字序列，供标题近似匹配使用。"""
    return re.sub(r"[^a-z0-9]", "", html.unescape(str(value)).lower())


# 标题相似度和年份一致仅是元数据检查，不等于文献支持具体医学陈述。
checked = source_reviews()
counts = Counter()
with (out / "dataset_audit.csv").open("w", encoding="utf-8-sig", newline="") as file:
    writer = csv.DictWriter(
        file,
        fieldnames=[
            "id",
            "name",
            "dataset_hash",
            "content_length",
            "reference_count",
            "references_metadata_verified",
            "references_title_difference",
            "references_year_difference",
            "references_pending_or_missing",
            "treatment_text_present",
            "source_review",
            "clinical_review",
        ],
    )
    writer.writeheader()
    for row in rows:
        verified = titles = years = pending = 0
        for ref in row["references"]:
            actual = cache.get(str(ref.get("pmid", "")), {})
            if not actual or "error" in actual:
                pending += 1
                continue
            similar = SequenceMatcher(
                None,
                normalize(ref.get("title", "")),
                normalize(actual.get("title", "")),
            ).ratio()
            same_year = str(ref.get("year", "")) in (
                actual.get("pubdate", "") + " " + actual.get("epubdate", "")
            )
            titles += similar < 0.9
            years += not same_year
            verified += similar >= 0.9 and same_year
        treatment = bool(
            re.search(r"treat|therapy|dose|first.line|mg\b", row["content"], re.I)
        )
        counts.update(
            references_verified=verified,
            title_differences=titles,
            year_differences=years,
            pending_or_missing=pending,
            records_with_treatment=treatment,
        )
        writer.writerow(
            {
                "id": row["id"],
                "name": row["name"],
                "dataset_hash": row["dataset_hash"],
                "content_length": len(row["content"]),
                "reference_count": len(row["references"]),
                "references_metadata_verified": verified,
                "references_title_difference": titles,
                "references_year_difference": years,
                "references_pending_or_missing": pending,
                "treatment_text_present": treatment,
                "source_review": (
                    "source_checked" if row["id"] in checked else "unreviewed"
                ),
                "clinical_review": "pending",
            }
        )
report = {
    "records": len(rows),
    "unique_pmids": len(pmids),
    "metadata_returned": sum(
        "error" not in cache.get(p, {"error": "pending"}) for p in pmids
    ),
    "counts": dict(counts),
    "source_checked_records": len(checked),
    "clinical_reviewed_records": 0,
    "request_errors": errors,
    "interpretation": "Metadata differences need investigation; not proof of fabricated references. Metadata agreement does not establish clinical claim support or dataset licensing.",
}
# 原始文件另做结构、许可字段及药物字段异常统计，不据此认定医学真伪。
raw_path = Path("C:/Users/fyy31/Desktop/疾病对话数据集.jsonl")
raw_counts = Counter()
raw_anomalies = []
names = {row["name"].casefold() for row in rows}
for line in raw_path.read_text(encoding="utf-8-sig").splitlines():
    try:
        record = json.loads(line)
    except json.JSONDecodeError:
        raw_counts["invalid_json"] += 1
        continue
    raw_counts["records"] += 1
    raw_counts["records_without_license_field"] += "license" not in record
    raw_counts["records_with_persona_or_scenario"] += bool(
        record.get("clinician_persona") or record.get("patient_scenario")
    )
    raw_counts["alias_links_to_other_record_names"] += sum(
        alias.casefold() in names and alias.casefold() != record["name"].casefold()
        for alias in record["aliases"]
    )
    for index, drug in enumerate(record.get("related_drugs", [])):
        extra = set(drug) - {"din", "brand_name", "ingredient", "atc_code", "role"}
        raw_counts["drug_records_with_unexpected_keys"] += bool(extra)
        if extra:
            raw_anomalies.append(
                {
                    "disease": record["name"],
                    "drug_index": index,
                    "unexpected_keys": sorted(extra),
                }
            )
report["raw_file_checks"] = dict(raw_counts)
(out / "raw_anomalies.json").write_text(
    json.dumps(raw_anomalies, ensure_ascii=False, indent=2), encoding="utf-8"
)
report["metadata_method"] = (
    "NLM PubMed esummary; title normalized similarity >=0.9 plus year present in publication/epublication date; not a claim-entailment or licensing review."
)
(out / "audit_summary.json").write_text(
    json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
)
print(json.dumps(report, ensure_ascii=False), flush=True)
