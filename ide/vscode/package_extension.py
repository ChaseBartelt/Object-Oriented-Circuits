"""Build the local OOC VS Code extension package without npm dependencies."""

import argparse
import json
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree as ET
from zipfile import ZIP_DEFLATED, ZipFile


EXTENSION = Path(__file__).resolve().parent
REPO = EXTENSION.parents[1]
VSIX_SCHEMA = "http://schemas.microsoft.com/developer/vsx-schema/2011"
CONTENT_SCHEMA = "http://schemas.openxmlformats.org/package/2006/content-types"


def vsix_manifest(package):
    root = ET.Element("PackageManifest", Version="2.0.0", xmlns=VSIX_SCHEMA)
    metadata = ET.SubElement(root, "Metadata")
    ET.SubElement(
        metadata,
        "Identity",
        Id=package["name"],
        Version=package["version"],
        Publisher=package["publisher"],
        Language="en-US",
    )
    ET.SubElement(metadata, "DisplayName").text = package["displayName"]
    ET.SubElement(metadata, "Description").text = package["description"]
    ET.SubElement(metadata, "Categories").text = ",".join(package.get("categories", ()))
    ET.SubElement(metadata, "License").text = "extension/LICENSE"
    properties = ET.SubElement(metadata, "Properties")
    ET.SubElement(
        properties,
        "Property",
        Id="Microsoft.VisualStudio.Code.Engine",
        Value=package["engines"]["vscode"],
    )
    installation = ET.SubElement(root, "Installation")
    ET.SubElement(installation, "InstallationTarget", Id="Microsoft.VisualStudio.Code")
    ET.SubElement(root, "Dependencies")
    assets = ET.SubElement(root, "Assets")
    for kind, path in (
        ("Manifest", "package.json"),
        ("Details", "README.md"),
        ("License", "LICENSE"),
    ):
        ET.SubElement(
            assets,
            "Asset",
            Type=f"Microsoft.VisualStudio.Code.{kind}",
            Path=f"extension/{path}",
            Addressable="true",
        )
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def content_types():
    root = ET.Element("Types", xmlns=CONTENT_SCHEMA)
    for suffix, content_type in (
        (".code-snippets", "application/json"),
        (".js", "text/javascript"),
        (".json", "application/json"),
        (".md", "text/markdown"),
        (".vsixmanifest", "text/xml"),
    ):
        ET.SubElement(root, "Default", Extension=suffix, ContentType=content_type)
    ET.SubElement(root, "Override", PartName="/extension/LICENSE", ContentType="text/plain")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def build(output):
    package_path = EXTENSION / "package.json"
    package = json.loads(package_path.read_text(encoding="utf-8"))
    files = {
        "package.json": package_path,
        "README.md": EXTENSION / "README.md",
        "LICENSE": REPO / "LICENSE",
        "extension.js": EXTENSION / "extension.js",
        "structure.js": EXTENSION / "structure.js",
        "language-configuration.json": EXTENSION / "language-configuration.json",
        "syntaxes/OOC.json": REPO / "ide/visual-studio/Syntaxes/OOC.json",
    }
    for contribution in package["contributes"].get("snippets", ()):
        path = PurePosixPath(contribution["path"])
        if path.parts[0] != "snippets" or ".." in path.parts:
            raise ValueError(f"Unexpected snippet path {path}")
        files[path.as_posix()] = EXTENSION / path

    files = {name: path.read_bytes() for name, path in files.items()}
    output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types())
        archive.writestr("extension.vsixmanifest", vsix_manifest(package))
        for name, data in files.items():
            archive.writestr(f"extension/{name}", data)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="Where to write the .vsix package")
    output = parser.parse_args().output
    build(output)
    print(output.resolve())


if __name__ == "__main__":
    main()
