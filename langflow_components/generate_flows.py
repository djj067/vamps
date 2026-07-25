"""
Generate importable Langflow 1.7.3 flow JSON for the VAMPS components.

Run inside the Langflow venv:
    python langflow_components/generate_flows.py

Produces, next to this file:
  - vamps_spec_flow.json    Chat Input -> VAMPS Spec Generator -> Chat Output
  - vamps_build_flow.json   Chat Input -> VAMPS Build Prototype -> Chat Output

We build each graph with Langflow's own Graph API, dump it, then convert the
backend dump into the frontend canvas format (node type "genericNode" + edges
with the exact `œ`-escaped handle strings the UI computes). The result is
round-trip validated through Graph.from_payload so import can't silently fail.
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE / "vamps"))

from lfx.graph import Graph
from lfx.components.input_output import ChatInput, ChatOutput, TextOutputComponent

from spec_generator import SpecGenerator
from build_prototype import BuildPrototype


def _compact(o):
    # Handle strings MUST be compact (no spaces) to match the id Langflow's canvas
    # computes for each handle; spaced strings make ReactFlow silently drop the edge.
    return json.dumps(o, sort_keys=True, separators=(",", ":")).replace('"', "œ")


def _to_frontend(d: dict, positions: dict) -> dict:
    nodes = {n["id"]: n for n in d["data"]["nodes"]}

    for n in d["data"]["nodes"]:
        n["type"] = "genericNode"
        n["selected"] = False
        n["dragging"] = False
        n["width"], n["height"] = 320, 360
        n["position"] = {"x": positions[n["id"]][0], "y": positions[n["id"]][1]}
        n["positionAbsolute"] = dict(n["position"])
        n["measured"] = {"width": 320, "height": 360}
    d["data"]["viewport"] = {"x": 0, "y": 0, "zoom": 0.6}

    def node(nid):
        return nodes[nid]["data"]["node"]

    def dtype(nid):
        return nodes[nid]["data"]["type"]

    def src_handle(nid, name):
        o = next(x for x in node(nid)["outputs"] if x["name"] == name)
        return {"dataType": dtype(nid), "id": nid, "name": name, "output_types": o["types"]}

    def tgt_handle(nid, field):
        t = node(nid)["template"][field]
        return {"fieldName": field, "id": nid, "inputTypes": t.get("input_types", []), "type": t.get("type")}

    new_edges = []
    for e in d["data"]["edges"]:
        s, t = e["source"], e["target"]
        sh = src_handle(s, e["data"]["sourceHandle"]["name"])
        th = tgt_handle(t, e["data"]["targetHandle"]["fieldName"])
        new_edges.append({
            "animated": False, "className": "",
            "data": {"sourceHandle": sh, "targetHandle": th},
            "id": f"reactflow__edge-{s}{_compact(sh)}-{t}{_compact(th)}",
            "selected": False,
            "source": s, "sourceHandle": _compact(sh),
            "target": t, "targetHandle": _compact(th),
        })
    d["data"]["edges"] = new_edges
    return d


def _build(name, description, nodes, edges, positions):
    """nodes: [(id, component)]; edges: [(src_id, out, tgt_id, in)]."""
    g = Graph()
    for nid, comp in nodes:
        g.add_component(comp, nid)
    for s, out, t, field in edges:
        g.add_component_edge(s, (out, field), t)
    g.prepare()   # infer start/stop; the graph may have multiple source/leaf nodes
    d = json.loads(g.dumps(name=name, description=description))
    d = _to_frontend(d, positions)

    # Round-trip validation: this rebuilds vertices (exec'ing embedded code) and edges.
    Graph.from_payload(d["data"], flow_id="validate").prepare()
    return d


def _set_field(flow, node_id, field, value):
    """Bake a value into a node's template field (e.g. Spec Generator's provider)."""
    for n in flow["data"]["nodes"]:
        if n["id"] == node_id:
            tmpl = n["data"]["node"]["template"]
            if field in tmpl:
                tmpl[field]["value"] = value
    return flow


def _full_pipeline(name, description, suffix, provider, model):
    """Chat Input (transcript) -> Spec -> Build -> Chat Output (html) + spec text.

    No Deepgram — Langflow can't take browser mic input, so the transcript is a
    text/chat input. `provider` is baked onto the Spec node ('claude_cli' or
    'openrouter').
    """
    ci, sp, bd, co, so = f"In-{suffix}", f"Spec-{suffix}", f"Build-{suffix}", f"Html-{suffix}", f"Spec-out-{suffix}"
    flow = _build(
        name, description,
        [(ci, ChatInput()), (sp, SpecGenerator()), (bd, BuildPrototype()),
         (co, ChatOutput()), (so, TextOutputComponent())],
        [(ci, "message", sp, "transcript"),
         (sp, "spec_text", bd, "spec"),
         (sp, "spec_text", so, "input_value"),
         (bd, "html", co, "input_value")],
        {ci: (60, 300), sp: (440, 260), bd: (860, 140), co: (1280, 140), so: (860, 540)},
    )
    _set_field(flow, sp, "provider", provider)
    if model:
        _set_field(flow, sp, "model", model)
    # Build via the same provider (Option B: openrouter build calls a Claude model
    # directly instead of the Claude Code CLI). Sonnet 5 for the HTML generation.
    _set_field(flow, bd, "provider", provider)
    if provider == "openrouter":
        _set_field(flow, bd, "model", "anthropic/claude-sonnet-5")
    return flow


def main():
    # --- API flows the webpage bridge (main.py) calls per /spec and /build.
    #     Names matter: main.py resolves the flow id by these display names,
    #     and passes provider/model as tweaks (OpenRouter key from Langflow env).
    spec_api = _build(
        "VAMPS Spec API",
        "Chat Input (transcript) -> Spec Generator -> Text Output (result JSON)",
        [("ChatInput-vsa", ChatInput()),
         ("Spec-vsa", SpecGenerator()),
         ("Out-vsa", TextOutputComponent())],
        [("ChatInput-vsa", "message", "Spec-vsa", "transcript"),
         ("Spec-vsa", "result_json", "Out-vsa", "input_value")],
        {"ChatInput-vsa": (100, 300), "Spec-vsa": (540, 260), "Out-vsa": (1020, 300)},
    )
    (HERE / "vamps_spec_api_flow.json").write_text(json.dumps(spec_api, indent=2))
    print("wrote vamps_spec_api_flow.json")

    build_api = _build(
        "VAMPS Build API",
        "Chat Input (spec) -> Build Prototype -> Text Output (result JSON incl. html)",
        [("ChatInput-vba", ChatInput()),
         ("Build-vba", BuildPrototype()),
         ("Out-vba", TextOutputComponent())],
        [("ChatInput-vba", "message", "Build-vba", "spec"),
         ("Build-vba", "result_json", "Out-vba", "input_value")],
        {"ChatInput-vba": (100, 300), "Build-vba": (540, 260), "Out-vba": (1020, 300)},
    )
    (HERE / "vamps_build_api_flow.json").write_text(json.dumps(build_api, indent=2))
    print("wrote vamps_build_api_flow.json")

    # --- the drag-in canvas/Playground flow (OpenRouter / Haiku), no Deepgram ---
    openrouter_full = _full_pipeline(
        "VAMPS Full (OpenRouter)",
        "Transcript -> Spec (OpenRouter) -> Build Prototype -> HTML + spec. Needs OPENROUTER_API_KEY.",
        "fo", provider="openrouter", model="anthropic/claude-haiku-4.5",
    )
    (HERE / "vamps_full_openrouter.json").write_text(json.dumps(openrouter_full, indent=2))
    print("wrote vamps_full_openrouter.json")


if __name__ == "__main__":
    main()
