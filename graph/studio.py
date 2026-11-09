"""Individual agent graphs exposed to LangGraph Studio (langgraph.json)."""
from graph.curator import build_curator_graph
from graph.extraction import build_extraction_graph
from graph.feedback import build_feedback_graph
from graph.triage import build_triage_graph

triage = build_triage_graph()
extraction = build_extraction_graph()
curator = build_curator_graph()
feedback = build_feedback_graph()
