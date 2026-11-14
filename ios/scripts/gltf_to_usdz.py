#!/usr/bin/env python3
"""Convert the living-room glTF binary (GLB) into a USDZ SceneKit can load.

The all-off export (room_corner.glb) was written with onlyVisible, so three.js
left out the glow decals and the fireplace flames. Those nodes exist only in
room_corner_all_on.glb. This script converts that file, keeps every node name
from devices.json, and writes appearance.json with the ON colours the app
applies when a device is on. The app then hides glows and flames and zeros
emission until the Hub says a device is on.

Requires the usd-core package (pip install usd-core). No Mac tools:

    python3 ios/scripts/gltf_to_usdz.py \\
        /path/to/room_corner_all_on.glb \\
        ios/Rooms/LivingRoom/room_corner.usdz \\
        ios/Rooms/LivingRoom/appearance.json

USD is Y-up and metres, same as the glTF. Texture V is flipped because glTF's
origin is the top of the image and USD's is the bottom.
"""

from __future__ import annotations

import json
import struct
import sys
from pathlib import Path

from pxr import Gf, Sdf, Usd, UsdGeom, UsdShade, UsdUtils, Vt

FLOAT = 5126
UBYTE = 5121
USHORT = 5123
UINT = 5125
COMPONENTS = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4}
UNPACK = {FLOAT: "f", UBYTE: "B", USHORT: "H", UINT: "I"}


def load_glb(path: Path) -> tuple[dict, bytes]:
    data = path.read_bytes()
    magic, _, length = struct.unpack_from("<4sII", data, 0)
    if magic != b"glTF":
        raise SystemExit(f"{path} is not a GLB")
    off = 12
    chunks: dict[bytes, bytes] = {}
    while off + 8 <= length:
        clen, ctype = struct.unpack_from("<I4s", data, off)
        off += 8
        chunks[ctype] = data[off : off + clen]
        off += clen
    return json.loads(chunks[b"JSON"]), chunks[b"BIN\x00"]


def read_accessor(gltf: dict, blob: bytes, index: int) -> list:
    acc = gltf["accessors"][index]
    view = gltf["bufferViews"][acc["bufferView"]]
    ctype = acc["componentType"]
    ncomp = COMPONENTS[acc["type"]]
    count = acc["count"]
    fmt = UNPACK[ctype]
    size = struct.calcsize(fmt)
    stride = view.get("byteStride") or size * ncomp
    start = view.get("byteOffset", 0) + acc.get("byteOffset", 0)
    out = []
    for i in range(count):
        base = start + i * stride
        vals = struct.unpack_from("<" + fmt * ncomp, blob, base)
        out.append(vals if ncomp > 1 else vals[0])
    return out


def sanitize(name: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in name)
    if not cleaned or cleaned[0].isdigit():
        cleaned = "n_" + cleaned
    return cleaned


def node_matrix(node: dict) -> Gf.Matrix4d:
    if "matrix" in node:
        m = node["matrix"]
        return Gf.Matrix4d(*m)
    t = node.get("translation", [0, 0, 0])
    r = node.get("rotation", [0, 0, 0, 1])  # xyzw
    s = node.get("scale", [1, 1, 1])
    # glTF quaternion is x, y, z, w. Gf.Quatd is real-first.
    quat = Gf.Quatd(r[3], r[0], r[1], r[2])
    rot = Gf.Matrix4d()
    rot.SetRotate(quat)
    mat = Gf.Matrix4d()
    mat.SetScale(Gf.Vec3d(*s))
    mat = mat * rot
    mat.SetTranslateOnly(Gf.Vec3d(*t))
    return mat


def kind_for(name: str) -> str | None:
    """Which nodes the app turns on and off. Names come from the three.js builders."""
    if name.startswith("glow_"):
        return "glow"
    if name.startswith("fire_flame"):
        return "flame"
    if name.startswith("light_cone_") or name.endswith("_beam") or name.startswith("light_shaft") or "spin_blur" in name:
        return "cone"
    if name == "fire_embers" or name.endswith("_embers"):
        return "embers"
    if name == "tv_screen":
        return "screen"
    if name == "soundbar_led" or "petal" in name or "_bloom" in name:
        return "led"
    if any(part in name for part in ("shade", "lining", "diffuser")) or name.endswith("_dome") or name.endswith("_bowl") or "glass_dome" in name:
        return "shade"
    if name == "cabinet_light_strip" or any(part in name for part in ("bulb", "puck", "_strip", "toe_kick", "_lamp", "_step", "_shelf")):
        return "bulb"
    if name.startswith("recessed_light") and "trim" not in name:
        return "bulb"
    if any(part in name for part in ("_display", "_door_glass", "_door_ring", "_status_arc")):
        return "led"
    return None


def linear_to_srgb(c: float) -> float:
    if c <= 0.0031308:
        return 12.92 * c
    return 1.055 * (c ** (1 / 2.4)) - 0.055


def srgb_tuple(rgb: list[float]) -> tuple[float, float, float]:
    return tuple(max(0.0, min(1.0, linear_to_srgb(float(c)))) for c in rgb[:3])


class Converter:
    def __init__(self, gltf: dict, blob: bytes, stage_path: Path, tex_dir: Path):
        self.gltf = gltf
        self.blob = blob
        self.tex_dir = tex_dir
        self.stage = Usd.Stage.CreateNew(str(stage_path))
        UsdGeom.SetStageUpAxis(self.stage, UsdGeom.Tokens.y)
        UsdGeom.SetStageMetersPerUnit(self.stage, 1)
        self.stage.SetDefaultPrim(self.stage.DefinePrim("/LivingRoom", "Xform"))
        self.used: set[str] = set()
        self.materials: dict[int, Sdf.Path] = {}
        self.textures: dict[int, str] = {}
        self.appearance: dict = {"nodes": {}, "lights": {}}

    def export_images(self) -> None:
        self.tex_dir.mkdir(parents=True, exist_ok=True)
        for i, image in enumerate(self.gltf.get("images", [])):
            view = self.gltf["bufferViews"][image["bufferView"]]
            start = view.get("byteOffset", 0)
            blob = self.blob[start : start + view["byteLength"]]
            ext = "png" if image.get("mimeType") == "image/png" else "jpg"
            filename = f"tex_{i}.{ext}"
            (self.tex_dir / filename).write_bytes(blob)
            self.textures[i] = filename

    def material(self, index: int) -> Sdf.Path:
        cached = self.materials.get(index)
        if cached is not None:
            return cached
        mat = self.gltf["materials"][index]
        path = Sdf.Path(f"/LivingRoom/Looks/mat_{index}")
        material = UsdShade.Material.Define(self.stage, path)
        shader = UsdShade.Shader.Define(self.stage, path.AppendChild("surface"))
        shader.CreateIdAttr("UsdPreviewSurface")
        pbr = mat.get("pbrMetallicRoughness", {})
        base = pbr.get("baseColorFactor", [1, 1, 1, 1])
        color = srgb_tuple(base)
        if "baseColorTexture" in pbr:
            tex_index = self.gltf["textures"][pbr["baseColorTexture"]["index"]]["source"]
            reader = self._texture(path, "diffuseTex", self.textures[tex_index])
            shader.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).ConnectToSource(
                reader.ConnectableAPI(), "rgb")
        else:
            shader.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).Set(Gf.Vec3f(*color))
        shader.CreateInput("roughness", Sdf.ValueTypeNames.Float).Set(float(pbr.get("roughnessFactor", 1)))
        shader.CreateInput("metallic", Sdf.ValueTypeNames.Float).Set(float(pbr.get("metallicFactor", 0)))
        opacity = float(base[3]) if len(base) > 3 else 1
        if mat.get("alphaMode") == "BLEND" or opacity < 0.999:
            shader.CreateInput("opacity", Sdf.ValueTypeNames.Float).Set(opacity)
        emissive = mat.get("emissiveFactor")
        strength = float(mat.get("extensions", {}).get("KHR_materials_emissive_strength", {}).get("emissiveStrength", 1 if emissive else 0))
        if emissive and strength > 0:
            # Keep the on-state in the file. The app multiplies it back down when the device is off.
            em = srgb_tuple(emissive)
            shader.CreateInput("emissiveColor", Sdf.ValueTypeNames.Color3f).Set(
                Gf.Vec3f(em[0] * strength, em[1] * strength, em[2] * strength))
        if "emissiveTexture" in mat:
            tex_index = self.gltf["textures"][mat["emissiveTexture"]["index"]]["source"]
            reader = self._texture(path, "emissiveTex", self.textures[tex_index])
            # A textured screen: the colour input is replaced by the texture. Strength stays as a note in appearance.json.
            shader.CreateInput("emissiveColor", Sdf.ValueTypeNames.Color3f).ConnectToSource(
                reader.ConnectableAPI(), "rgb")
        material.CreateSurfaceOutput().ConnectToSource(shader.ConnectableAPI(), "surface")
        self.materials[index] = path
        return path

    def _texture(self, parent: Sdf.Path, name: str, filename: str) -> UsdShade.Shader:
        reader = UsdShade.Shader.Define(self.stage, parent.AppendChild(name))
        reader.CreateIdAttr("UsdUVTexture")
        reader.CreateInput("file", Sdf.ValueTypeNames.Asset).Set(Sdf.AssetPath("./" + filename))
        reader.CreateInput("sourceColorSpace", Sdf.ValueTypeNames.Token).Set("sRGB")
        reader.CreateOutput("rgb", Sdf.ValueTypeNames.Float3)
        st = UsdShade.Shader.Define(self.stage, parent.AppendChild(name + "ST"))
        st.CreateIdAttr("UsdPrimvarReader_float2")
        st.CreateInput("varname", Sdf.ValueTypeNames.Token).Set("st")
        st.CreateOutput("result", Sdf.ValueTypeNames.Float2)
        reader.CreateInput("st", Sdf.ValueTypeNames.Float2).ConnectToSource(st.ConnectableAPI(), "result")
        return reader

    def write_mesh(self, prim_path: Sdf.Path, mesh_index: int, bind: Sdf.Path | None, double_sided: bool) -> None:
        mesh_def = self.gltf["meshes"][mesh_index]
        # The exporter writes one primitive per mesh. If a mesh has several, only the first is bound;
        # this model uses one primitive each.
        prim = mesh_def["primitives"][0]
        points = read_accessor(self.gltf, self.blob, prim["attributes"]["POSITION"])
        normals = read_accessor(self.gltf, self.blob, prim["attributes"]["NORMAL"]) if "NORMAL" in prim["attributes"] else None
        uvs = read_accessor(self.gltf, self.blob, prim["attributes"]["TEXCOORD_0"]) if "TEXCOORD_0" in prim["attributes"] else None
        indices = read_accessor(self.gltf, self.blob, prim["indices"]) if "indices" in prim else list(range(len(points)))
        usd_mesh = UsdGeom.Mesh.Define(self.stage, prim_path)
        usd_mesh.CreatePointsAttr(Vt.Vec3fArray([Gf.Vec3f(*p) for p in points]))
        usd_mesh.CreateFaceVertexCountsAttr(Vt.IntArray([3] * (len(indices) // 3)))
        usd_mesh.CreateFaceVertexIndicesAttr(Vt.IntArray([int(i) for i in indices]))
        usd_mesh.CreateSubdivisionSchemeAttr("none")
        if double_sided or self.gltf["materials"][prim.get("material", 0)].get("doubleSided"):
            usd_mesh.CreateDoubleSidedAttr(True)
        if normals:
            usd_mesh.CreateNormalsAttr(Vt.Vec3fArray([Gf.Vec3f(float(n[0]), float(n[1]), float(n[2])) for n in normals]))
            usd_mesh.SetNormalsInterpolation(UsdGeom.Tokens.vertex)
        if uvs:
            st = UsdGeom.PrimvarsAPI(usd_mesh).CreatePrimvar(
                "st", Sdf.ValueTypeNames.TexCoord2fArray, UsdGeom.Tokens.vertex)
            # glTF V is down the image; USD V is up.
            st.Set(Vt.Vec2fArray([Gf.Vec2f(float(u[0]), 1.0 - float(u[1])) for u in uvs]))
        if bind is not None:
            UsdShade.MaterialBindingAPI(usd_mesh).Bind(UsdShade.Material(self.stage.GetPrimAtPath(bind)))

    def note_appearance(self, name: str, node: dict) -> None:
        kind = kind_for(name)
        if kind is None or "mesh" not in node:
            return
        prim = self.gltf["meshes"][node["mesh"]]["primitives"][0]
        mat = self.gltf["materials"][prim.get("material", 0)]
        pbr = mat.get("pbrMetallicRoughness", {})
        base = pbr.get("baseColorFactor", [1, 1, 1, 1])
        emissive = mat.get("emissiveFactor") or [0, 0, 0]
        strength = float(mat.get("extensions", {}).get("KHR_materials_emissive_strength", {}).get(
            "emissiveStrength", 1 if any(emissive) else 0))
        entry: dict = {
            "kind": kind,
            "base_srgb": list(srgb_tuple(base)),
            "opacity": float(base[3]) if len(base) > 3 else 1,
            "emissive_srgb": list(srgb_tuple(emissive)),
            "strength": strength,
            "roughness": float(pbr.get("roughnessFactor", 1)),
            "metallic": float(pbr.get("metallicFactor", 0)),
            "double_sided": bool(mat.get("doubleSided")),
        }
        if "baseColorTexture" in pbr:
            entry["base_texture"] = self.textures[self.gltf["textures"][pbr["baseColorTexture"]["index"]]["source"]]
        if "emissiveTexture" in mat:
            entry["emissive_texture"] = self.textures[self.gltf["textures"][mat["emissiveTexture"]["index"]]["source"]]
        self.appearance["nodes"][name] = entry

    def note_light(self, name: str, light_index: int) -> None:
        light = self.gltf["extensions"]["KHR_lights_punctual"]["lights"][light_index]
        entry = {
            "type": light["type"],
            "color_srgb": list(srgb_tuple(light.get("color", [1, 1, 1]))),
            "intensity": float(light.get("intensity", 0)),
            "range": float(light.get("range", 0)),
        }
        if "spot" in light:
            entry["inner_deg"] = float(light["spot"]["innerConeAngle"]) * 180 / 3.14159265
            entry["outer_deg"] = float(light["spot"]["outerConeAngle"]) * 180 / 3.14159265
        self.appearance["lights"][name] = entry


def main() -> None:
    if len(sys.argv) != 4:
        raise SystemExit("usage: gltf_to_usdz.py input.glb output.usdz appearance.json")
    src, dst, appearance_path = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
    gltf, blob = load_glb(src)
    work = dst.parent / "_usdz_build"
    if work.exists():
        for child in work.iterdir():
            child.unlink()
    else:
        work.mkdir(parents=True)
    stage_path = work / "room.usdc"
    converter = Converter(gltf, blob, stage_path, work)
    _build(converter)
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst.unlink()
    if not UsdUtils.CreateNewUsdzPackage(str(stage_path), str(dst)):
        raise SystemExit("USDZ packaging failed")
    appearance_path.write_text(json.dumps(converter.appearance, indent=2) + "\n")
    opened = Usd.Stage.Open(str(dst))
    if not opened:
        raise SystemExit(f"could not reopen {dst}")
    names = {p.GetName() for p in opened.Traverse()}
    anchors = [n for n in names if n.startswith("anchor_")]
    # The living room is the room with the chandelier; the bedroom also has a TV now.
    if "anchor_chandelier" in names:
        missing = [n for n in ("tv_screen", "glow_chandelier_floor", "fire_flame_1", "soundbar_led", "light_chandelier") if n not in names]
    elif "anchor_washer" in names:
        missing = [n for n in ("anchor_under_cabinet_lights", "glow_washer_drum", "light_laundry") if n not in names]
    else:
        missing = []
    if len(anchors) < 3:
        missing.append("anchors")
    if missing:
        raise SystemExit(f"USDZ is missing nodes: {missing}")
    print(f"wrote {dst} ({dst.stat().st_size} bytes), {len(converter.appearance['nodes'])} lit nodes, "
          f"{len(converter.appearance['lights'])} lights")
    textures = dst.parent / "textures"
    textures.mkdir(exist_ok=True)
    for child in work.iterdir():
        if child.suffix == ".png":
            (textures / child.name).write_bytes(child.read_bytes())
        child.unlink()
    work.rmdir()


def _build(self: Converter) -> None:
    """Hierarchy walk with stable prim names.

    The first node to use a name keeps it, so devices.json names survive. A repeated
    name (four sofa legs, two wall panels) gets a numeric suffix. A dot in a name
    would be a USD path separator, so it becomes an underscore.
    """
    self.export_images()
    self.used = set()
    root = self.gltf["scenes"][self.gltf["scene"]]["nodes"]

    def claim(raw: str) -> str:
        base = sanitize(raw)
        if base not in self.used:
            self.used.add(base)
            return base
        n = 2
        while f"{base}_{n}" in self.used:
            n += 1
        name = f"{base}_{n}"
        self.used.add(name)
        return name

    def walk(index: int, parent: Sdf.Path) -> None:
        node = self.gltf["nodes"][index]
        raw = node.get("name") or f"node_{index}"
        name = claim(raw)
        path = parent.AppendChild(name)
        xform = UsdGeom.Xform.Define(self.stage, path)
        xform.AddTransformOp().Set(node_matrix(node))
        if name == raw or name == sanitize(raw):
            self.note_appearance(name, node)
            light_ext = node.get("extensions", {}).get("KHR_lights_punctual")
            if light_ext is not None:
                self.note_light(name, light_ext["light"])
        if "mesh" in node:
            prim = self.gltf["meshes"][node["mesh"]]["primitives"][0]
            mat_index = prim.get("material")
            bind = self.material(mat_index) if mat_index is not None else None
            double = bool(self.gltf["materials"][mat_index].get("doubleSided")) if mat_index is not None else False
            self.write_mesh(path.AppendChild("mesh"), node["mesh"], bind, double)
        for child in node.get("children", []):
            walk(child, path)

    for index in root:
        walk(index, Sdf.Path("/LivingRoom"))
    self.stage.GetRootLayer().Save()


if __name__ == "__main__":
    main()
