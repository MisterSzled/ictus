"""What `ictus init` writes into a new pipeline folder."""

from __future__ import annotations

__all__ = ["STARTER_INPUT", "STARTER_PIPELINE"]

STARTER_INPUT = """---
# Every key here must be an input the pipeline declares.
# `repo:` is the exception: it says which directory the run works in,
# relative to this file. Leave it out to work in the directory you launch from.
---
The body feeds whichever input is declared with prose=True.
"""

STARTER_PIPELINE = '''"""What this pipeline does."""

from __future__ import annotations

from ictus import END, AgentNode, InputPort, OutputPort, Pipeline, PortType, tpl

STR = PortType.STRING

pipeline = Pipeline(pipeline_id="CHANGE-ME", description="What this does")
brief = pipeline.declare_input("brief", STR, prose=True, description="What to work from")

work = pipeline.add(
    AgentNode(
        node_id="work",
        description="The first step",
        inputs=(InputPort("brief", STR),),
        prompt=tpl("Do the thing described below.\\n\\n", brief.ref()),
        declared_outputs=(OutputPort("result", STR, "What it produced"),),
    )
)
pipeline.set_entry(work)
pipeline.connect_input(brief, work, "brief")
pipeline.route(work, END)
'''
