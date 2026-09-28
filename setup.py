"""Bundle connection assets from their maintained source files."""
from pathlib import Path
import shutil

from setuptools import setup
from setuptools.command.build_py import build_py


ASSETS = (
    "tools/templates/project_launcher.py.in",
    "tools/role_flow.py",
    "tools/project_overview.py",
    "tools/role_review.py",
    "tools/role_manage.py",
    "tools/flow_archive.py",
    "tools/flow_lifecycle.py",
    "tools/flow_cleanup.py",
    "tools/flow_archive_writer.py",
    "skills/peer-consult/SKILL.md",
    "skills/peer-consult/references/usage.md",
    "skills/peer-consult/references/roles.md",
)


class BuildWithConnectionAssets(build_py):
    def run(self):
        super().run()
        source = Path(__file__).resolve().parent
        target = Path(self.build_lib) / "agent_bridge" / "_connect_assets"
        for relative in ASSETS:
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / relative, destination)

    def get_outputs(self, include_bytecode=1):
        outputs = super().get_outputs(include_bytecode)
        target = Path(self.build_lib) / "agent_bridge" / "_connect_assets"
        return outputs + [str(target / relative) for relative in ASSETS]


setup(cmdclass={"build_py": BuildWithConnectionAssets})
