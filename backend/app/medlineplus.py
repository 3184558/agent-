"""只导入官方健康主题摘要；保留 XML、原始 HTML 和可定位的派生纯文本。"""

import hashlib
import json
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path

from . import database
from .knowledge import field_document, save_documents

ATTRIBUTION = "Source: MedlinePlus, National Library of Medicine."
LICENSE = "Health topic summaries: public domain; excludes external sites, A.D.A.M., drugs and images."


class SummaryText(HTMLParser):
    """保留正文段落边界，使用标准库处理实体和内联标签。"""

    def __init__(self):
        """启用实体解码，按出现顺序保存正文片段。"""
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_starttag(self, tag, attrs):
        """在块级标签之间保留换行，避免相邻段落粘连。"""
        if tag in ("p", "li", "h2", "h3", "br"):
            self.parts.append("\n")

    def handle_data(self, data):
        """保存文本，统一的空白整理在导入入口完成。"""
        self.parts.append(data)


def import_snapshot(xml_path: Path, selection: dict, download: dict):
    """验证下载记录，保存所选主题快照，只更新候选清单。"""
    raw = xml_path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != download["sha256"] or any(
        kind not in ("disease", "symptom", "test", "health")
        for kind in selection.values()
    ):
        raise ValueError("下载哈希或主题类型不符合导入契约")
    root = ET.fromstring(raw)
    topics = {
        t.attrib["title"]: t
        for t in root.findall("health-topic")
        if t.attrib["language"] == "English"
    }
    missing = sorted(set(selection) - topics.keys())
    loaded = []
    for title, kind in selection.items():
        if title not in topics:
            continue
        topic = topics[title]
        html = topic.findtext("full-summary", "")
        parser = SummaryText()
        parser.feed(html)
        text = "\n\n".join(
            " ".join(line.split())
            for line in "".join(parser.parts).splitlines()
            if line.strip()
        )
        if not text:
            raise ValueError(f"主题 {title} 没有摘要")
        loaded.append(
            field_document(
                text,
                family="medlineplus_summary",
                parent_id="medlineplus:" + topic.attrib["id"],
                field="full-summary",
                dataset_hash=digest,
                name=title,
                source_id="medlineplus-health-topics",
                topic_id=topic.attrib["id"],
                topic_type=kind,
                language="en",
                source_url=topic.attrib["url"],
                source_title=title + " — MedlinePlus",
                source_updated_at=None,
                source_created_at=topic.attrib.get("date-created"),
                downloaded_at=download["downloaded_at"],
                xml_generated_at=root.attrib["date-generated"],
                xml_path=str(xml_path.resolve()),
                raw_summary_html=html,
                aliases=[
                    a.text
                    for a in topic.findall("also-called")
                    + topic.findall("see-reference")
                ],
                groups=[g.text for g in topic.findall("group")],
                license=LICENSE,
                attribution=ATTRIBUTION,
                verification="official_public_summary",
                clinical_review="pending",
                derived_fields={
                    "plain_text": "html-parser-v1",
                    "translation": "not_generated",
                    "topic_type": "developer_assigned_pending_review",
                },
            )
        )
    with database.engine.begin() as connection:
        save_documents(connection, loaded)
    release = {
        "dataset_hash": digest,
        "documents": len(loaded),
        "missing_titles": missing,
        "document_versions": [d.metadata["document_version"] for d in loaded],
        "download": download,
    }
    directory = database.settings.data_dir / "sources/medlineplus"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{digest}.json").write_text(
        json.dumps(release, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary = directory / "candidate.pending.json"
    temporary.write_text(
        json.dumps(release, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(directory / "candidate.json")
    return release
