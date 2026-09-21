"""What every deployment needs, so the five agent files stay about the size of
their difference.

Only data and image configuration lives here. The agent body itself stays in
each agent file's `__main__`: cloudpickle serialises a function defined there
by value, and one imported from a module by reference — and a reference is
only as good as the module being present in the image under the same name.
The body is the one thing worth not being clever about.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Everything the catalogue is made of lives in assets/, inside the project.
# It used to point at an absolute path on one developer's machine, which made
# the repository unusable anywhere else; the client library is six megabytes,
# so vendoring it costs nothing and buys portability.
ASSETS = ROOT / "assets"

# Shared by every engine. shapely and scipy carry the geometry; ezdxf reads and
# writes; matplotlib renders. None of it needs a GPU, which is the constraint
# that ruled out most of the published work in this space.
BASE_PIP = ("ezdxf", "shapely>=2.0", "numpy", "scipy", "matplotlib",
            # The model runs through the Claude Code CLI on a subscription
            # token, not through the REST API — see core/sdk.py.
            "claude-agent-sdk", "fal-client")


def image(*extra_pip):
    import cycls
    img = (cycls.Image()
           .pip(*BASE_PIP, *extra_pip)
           # Without a real font, every label on the plan renders as a row of
           # tofu boxes and the delivered DXF is unreadable.
           .apt("fonts-dejavu-core")
           # The SDK spawns the Claude Code CLI, so the CLI has to be in the
           # image; it is a node package, and the base image has no node.
           .run("curl -fsSL https://deb.nodesource.com/setup_22.x | bash - "
                "&& apt-get install -y nodejs "
                "&& npm install -g @anthropic-ai/claude-code")
           .copy(str(ROOT / "core"), "core")
           # The two reference files the v2 prompt tells the model to Read.
           # Shipped rather than fetched: an agent that needs the network to
           # know how many toilets a crowd needs is one that fails offline.
           .copy(str(ROOT / "reference"), "reference")
           .copy(str(ROOT / "engines"), "engines")
           .copy(str(ASSETS), "assets")
           .copy(str(ROOT / ".env"), ".env"))
    return img


def web(title):
    import cycls
    # Auth mounts /files, which is where an attachment arrives and where the
    # finished DXF has to appear for the user to download it.
    return cycls.Web().auth(cycls.Clerk()).title(title)


def volumes(name):
    import cycls
    return {"/workspace": cycls.Volume(name)}


def finalise(agent):
    """Cloud Run allows 3600 s; cycls writes 1200 s into the deploy payload
    (_function/main.py) and `Agent.__init__` never plumbs a timeout through.
    The payload spreads **self.spec after its own default, so setting it on
    the returned Agent wins. A 7 MB masterplan and a real programme do not fit
    in twenty minutes."""
    agent.spec.update(timeout=3600)
    return agent
