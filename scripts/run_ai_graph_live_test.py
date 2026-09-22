from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import networkx as nx
from PIL import Image, ImageDraw, ImageFont

from historybot.ai_ingestion import AIIngestionService
from historybot.model_gateway import ModelResult
from historybot.store import KnowledgeStore
from historybot.v2_adapters import SQLiteMemoryStore, WikiStore
from historybot.v2_schemas import ReadingResponse, VerificationResponse
from historybot.web import ResearchView


ROOT = Path(__file__).resolve().parents[1]
SOURCE_DB = ROOT / "data" / "ai_ingestion_live_test.db"
TEST_DB = ROOT / "data" / "ai_graph_live_test.db"
WIKI_ROOT = ROOT / "artifacts" / "ai_graph_live_wiki"
REPORT = ROOT / "artifacts" / "ai_graph_live_report.json"
GRAPH = ROOT / "artifacts" / "ai_graph_live_projection.json"
IMAGE = ROOT / "artifacts" / "ai_graph_live_projection.png"


CASES = {
    "p_81fc340fa6d0fbe6": ("曹操籍贯沛国谯县，名操，字孟德，史文称其为曹参之后。", ["曹操", "曹参"], "身份与家世", "曹操", "籍贯", "沛国谯县"),
    "p_145a5d45afaa46c1": ("桓帝时曹腾任中常侍大长秋，封费亭侯。", ["曹腾"], "任官与封爵", "曹腾", "封爵", "费亭侯"),
    "p_4ad82257bf11d11d": ("曹嵩为曹腾养子，承袭爵位，官至太尉；其生父来历无法审知。", ["曹嵩", "曹腾"], "身份不确定性", "曹嵩", "养父", "曹腾"),
    "p_5a7fbf84e29d907e": ("曹操年轻时机警有权谋；桥玄和何颙认为他不同寻常。", ["曹操", "桥玄", "何颙"], "人物评价", "桥玄", "评价", "曹操不同寻常"),
    "p_84c2619bb8eac66d": ("桥玄判断天下将乱，并称能安定天下者或为曹操。", ["桥玄", "曹操"], "人物评价", "桥玄", "预言安定天下者", "曹操"),
    "p_3d67bfd5eb6d0fa0": ("曹操二十岁举孝廉为郎，历任洛阳北部尉、顿丘令，后征拜议郎。", ["曹操"], "任官经历", "曹操", "征拜", "议郎"),
    "p_8f14109733a594cd": ("曹操任济南相时奏免八名贪污长吏并禁断淫祀。", ["曹操"], "治理事件", "曹操", "治理", "济南"),
    "p_53bb20a56f810492": ("曹操被征为东郡太守，但未赴任，以病归乡。", ["曹操"], "任官与辞任", "曹操", "拒任", "东郡太守"),
    "p_5288e05bcd6d4d6c": ("王芬、许攸、周旌等谋废灵帝、立合肥侯；曹操拒绝参与。", ["王芬", "许攸", "周旌", "曹操", "汉灵帝"], "政治事件", "曹操", "拒绝参与", "废灵帝立合肥侯之谋"),
    "p_8c5c70ed757468bd": ("汉灵帝去世，太子即位，太后临朝。", ["汉灵帝", "太子", "太后"], "帝位更替", "太后", "临朝", "太子即位之后"),
    "p_60eda70ab150f349": ("何进与袁绍谋划诛杀宦官，太后不接受。", ["何进", "袁绍", "太后"], "政治谋划", "何进", "与袁绍谋划", "诛杀宦官"),
    "p_3f451cfbf50a895a": ("何进召董卓以威胁太后；董卓未到，何进已被杀。", ["何进", "董卓", "太后"], "何进被杀", "何进", "召", "董卓"),
    "p_165f0c265428cf68": ("董卓到京后废帝为弘农王，另立献帝，京都大乱。", ["董卓", "弘农王", "汉献帝"], "废立事件", "董卓", "拥立", "汉献帝"),
    "p_2b4023c41b1b2e44": ("董卓上表任曹操为骁骑校尉，意图与他共谋政事。", ["董卓", "曹操"], "任官邀约", "董卓", "表任", "曹操为骁骑校尉"),
    "p_b7443c992302d880": ("曹操改易姓名，秘密向东返回。", ["曹操"], "逃离行动", "曹操", "行动", "改名东归"),
    "p_159d80fd25f2f2db": ("曹操经过中牟时被亭长怀疑并押往县衙，后经人求情获释。", ["曹操", "亭长"], "中牟被捕获释", "曹操", "获释地点", "中牟"),
    "p_b2db335c81ffddd4": ("曹操到陈留后散尽家财，集合义兵，准备讨伐董卓。", ["曹操", "董卓"], "起兵讨董", "曹操", "起兵讨伐", "董卓"),
    "p_b8ed31dd1cbe135b": ("当年冬十二月，曹操在己吾开始起兵。", ["曹操"], "起兵时间地点", "曹操", "起兵地点", "己吾"),
}

EVENT_META = {
    "p_81fc340fa6d0fbe6": {"place": "沛国谯县"},
    "p_3d67bfd5eb6d0fa0": {"place": "洛阳"},
    "p_8f14109733a594cd": {"place": "济南"},
    "p_53bb20a56f810492": {"place": "东郡"},
    "p_165f0c265428cf68": {"place": "京都"},
    "p_159d80fd25f2f2db": {"place": "中牟"},
    "p_b2db335c81ffddd4": {"place": "陈留"},
    "p_b8ed31dd1cbe135b": {"place": "己吾", "date_start": "冬十二月"},
}


class ReplayGateway:
    def __init__(self, store: KnowledgeStore):
        self.store = store
        self.pending_units: list[dict] = []

    async def structured(self, *, task, system, user, schema):
        if schema is ReadingResponse:
            ids = [line.split("]", 1)[0][1:] for line in user.splitlines() if line.startswith("[p_")]
            units = []
            for passage_id in ids:
                passage = self.store.db.execute("SELECT payload FROM passages WHERE id=?", (passage_id,)).fetchone()
                passage_payload = json.loads(passage[0])
                summary, people, event_type, subject, predicate, object_ = CASES[passage_id]
                aliases = []
                if passage_id == "p_81fc340fa6d0fbe6":
                    aliases = [
                        {"canonical_surface": "曹操", "alias_surface": "太祖", "relation": "简称", "passage_id": passage_id, "confidence": 0.99},
                        {"canonical_surface": "曹操", "alias_surface": "孟德", "relation": "字", "passage_id": passage_id, "confidence": 0.99},
                    ]
                if passage_id == "p_3f451cfbf50a895a":
                    aliases.append({"canonical_surface": "董卓", "alias_surface": "卓", "relation": "简称", "passage_id": passage_id, "confidence": 0.99})
                unit = {
                    "unit_id": f"unit_{passage_id}",
                    "passage_id": passage_id,
                    "reading_summary": summary,
                    "explicit_facts": [summary],
                    "uncertainties": ["原文明确称曹嵩生父来历不可审知"] if passage_id == "p_4ad82257bf11d11d" else [],
                    "mentions": [{"surface": name, "entity_type": "人物", "passage_id": passage_id, "confidence": 0.98} for name in people],
                    "identity_links": aliases,
                    "events": [{"event_type": event_type, "label": summary, "participant_surfaces": people, "passage_id": passage_id, "confidence": 0.96, **EVENT_META.get(passage_id, {})}],
                    "claims": [{"id": f"model_{passage_id}", "subject": subject, "predicate": predicate, "object": object_, "statement": summary, "passage_ids": [passage_id], "confidence": 0.96}],
                    "reasoning_summary": "主张和事件均由该段原文直接支持。",
                    "evidence_quotes": [passage_payload["text"]],
                }
                units.append(unit)
            self.pending_units.extend(units)
            value = ReadingResponse(units=units, document_summary="；".join(item["reading_summary"] for item in units))
        else:
            value = VerificationResponse(
                items=[{"unit_id": item["unit_id"], "supported": True, "rationale": "引文与原段逐字匹配，结构化主张未超出原文。"} for item in self.pending_units],
                overall_supported=True,
                summary="逐段复核通过。",
            )
            self.pending_units = []
        return ModelResult(value=value, provider="codex-manual-replay", model="codex-manual-replay", input_tokens=0, output_tokens=0, raw={})


def prepare_database() -> KnowledgeStore:
    if TEST_DB.exists():
        TEST_DB.unlink()
    source = KnowledgeStore(str(SOURCE_DB))
    target = KnowledgeStore(str(TEST_DB))
    for source_id in ("src_live_caocao_identity", "src_live_dongzhuo"):
        target.save_source(source.get_source(source_id))
        target.save_passages(source.get_passages(source_id))
    return target


def render_claim_graph(graph: dict) -> None:
    edges = [edge for edge in graph["edges"] if edge.get("edge_type") == "claim"]
    network = nx.DiGraph()
    for edge in edges:
        network.add_edge(edge["source"], edge["target"], label=edge["label"])
    positions = nx.spring_layout(network, seed=19, k=1.45, iterations=180)
    width, height = 1800, 1200
    image = Image.new("RGB", (width, height), "#f5f6f8")
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 20)
    bold = ImageFont.truetype("C:/Windows/Fonts/msyhbd.ttc", 21)
    title = ImageFont.truetype("C:/Windows/Fonts/msyhbd.ttc", 36)
    draw.text((48, 30), "AI 阅读后生成的可追溯主张图", font=title, fill="#17283c")
    draw.text((50, 82), "18 段原文 / 18 条主张 / 每条边可回查 evidence 与 passage", font=font, fill="#596a7f")
    xs = [float(value[0]) for value in positions.values()]
    ys = [float(value[1]) for value in positions.values()]
    min_x, max_x, min_y, max_y = min(xs), max(xs), min(ys), max(ys)
    def point(name: str) -> tuple[int, int]:
        x, y = positions[name]
        px = 110 + int((float(x) - min_x) / max(max_x - min_x, 0.01) * (width - 220))
        py = 160 + int((float(y) - min_y) / max(max_y - min_y, 0.01) * (height - 260))
        return px, py
    for edge in edges:
        start, end = point(edge["source"]), point(edge["target"])
        draw.line((*start, *end), fill="#a9b2bd", width=2)
        middle = ((start[0] + end[0]) // 2, (start[1] + end[1]) // 2)
        label = edge["label"][:10]
        box = draw.textbbox(middle, label, font=font, anchor="mm")
        draw.rounded_rectangle((box[0] - 5, box[1] - 3, box[2] + 5, box[3] + 3), radius=5, fill="#f5f6f8")
        draw.text(middle, label, font=font, fill="#8b5a2b", anchor="mm")
    subjects = {edge["source"] for edge in edges}
    for node in network.nodes:
        x, y = point(node)
        label = node if len(node) <= 10 else node[:9] + "…"
        text_box = draw.textbbox((x, y), label, font=bold, anchor="mm")
        padding_x = 13
        color = "#2d7895" if node in subjects else "#66558c"
        draw.rounded_rectangle((text_box[0] - padding_x, text_box[1] - 9, text_box[2] + padding_x, text_box[3] + 9), radius=7, fill="white", outline=color, width=3)
        draw.text((x, y), label, font=bold, fill=color, anchor="mm")
    image.save(IMAGE)


async def main() -> None:
    store = prepare_database()
    if WIKI_ROOT.exists():
        shutil.rmtree(WIKI_ROOT)
    service = AIIngestionService(store, gateway=ReplayGateway(store), wiki=WikiStore(WIKI_ROOT), memories=SQLiteMemoryStore(store.db))
    runs = []
    for source_id in ("src_live_caocao_identity", "src_live_dongzhuo"):
        runs.append(await service.run(job_id=None, source_id=source_id, question="完整阅读并建立可追溯历史知识图谱"))

    view = ResearchView(TEST_DB).workspace()
    GRAPH.write_text(json.dumps(view["graph"], ensure_ascii=False, indent=2), encoding="utf-8")
    render_claim_graph(view["graph"])
    connection = store.db
    integrity = {
        "relations_missing_claim": connection.execute("SELECT COUNT(*) FROM relations r LEFT JOIN claims c ON c.id=r.claim_id WHERE c.id IS NULL").fetchone()[0],
        "claims_without_evidence": connection.execute("SELECT COUNT(*) FROM claims c LEFT JOIN evidence e ON e.claim_id=c.id WHERE e.id IS NULL").fetchone()[0],
        "evidence_missing_passage": connection.execute("SELECT COUNT(*) FROM evidence e LEFT JOIN passages p ON p.id=e.passage_id WHERE p.id IS NULL").fetchone()[0],
        "passages_without_claim_evidence": connection.execute("SELECT COUNT(*) FROM passages p LEFT JOIN evidence e ON e.passage_id=p.id WHERE e.id IS NULL").fetchone()[0],
    }
    report = {
        "mode": "codex-manual-replay",
        "source_note": "真实《三国志》段落；外部模型网关未参与本次成功运行。",
        "runs": runs,
        "database_counts": store.counts(),
        "graph_counts": {"nodes": len(view["graph"]["nodes"]), "edges": len(view["graph"]["edges"])},
        "integrity": integrity,
        "evidence_coverage": view["overview"]["evidence_coverage"],
        "aliases": [json.loads(row[0]) for row in connection.execute("SELECT payload FROM entity_aliases ORDER BY id")],
        "sample_claims": view["claims"][:6],
    }
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
