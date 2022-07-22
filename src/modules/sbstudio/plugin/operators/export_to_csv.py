"""This file is mostly a copy of addons/io_export_skybrush_csv.py, which
will probably be deprecated soon as its functionality is merged with the
"main" Blender addon.

The operator provided in this file exports drone show trajectories and light
animation to a simple (zipped) .csv format compatible with the Skybrush suite.

The primary and recommended drone show format of the Skybrush suite is the
Skybrush Compiled Format (.skyc), which is much more versatile and optimized
than the simple text output generated by this script.

This script is created for those who want to use their own scripts or tools for
post-processing.
"""

import bpy
import logging
import os
import re

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple
from zipfile import ZipFile, ZIP_DEFLATED

from bpy.props import BoolProperty, StringProperty, FloatProperty
from bpy.types import Operator
from bpy_extras.io_utils import ExportHelper

from sbstudio.plugin.props.frame_range import FrameRangeProperty, resolve_frame_range

from .export_to_skyc import _to_int_255, get_drones_to_export

__all__ = ("SkybrushCSVExportOperator",)

log = logging.getLogger(__name__)

#############################################################################
# some global variables that could be parametrized if needed

SUPPORTED_TYPES = ("MESH",)  # ,'CURVE','EMPTY','TEXT','CAMERA','LAMP')


#############################################################################
# Helper functions and classes for the exporter
#############################################################################


@dataclass
class TimePosColor:
    # time in milliseconds
    t: int
    # x position in meters
    x: float
    # y position in meters
    y: float
    # z position in meters
    z: float
    # red channel value [0-255]
    R: int
    # green channel value [0-255]
    G: int
    # blue channel value [0-255]
    B: int

    def __repr__(self):
        return f"{self.t},{round(self.x, ndigits=3)},{round(self.y, ndigits=3)},{round(self.z, ndigits=3)},{self.R},{self.G},{self.B}"


def _get_location(obj):
    """Return global location of an object at the actual frame.

    Parameters:
        obj: a Blender object

    Return:
        location of object in the world frame

    """
    return tuple(obj.matrix_world.translation)


def _find_shader_node_by_name_and_type(material, name: str, type: str):
    """Finds the first shader node with the given name and expected type in the
    shader node tree of the given material.

    Lookup by name will likely fail if Blender is localized; in this case we
    will return the _first_ shader node that matches the given type.

    Parameters:
        name: the name of the shader node
        type: the expected type of the shader node

    Raises:
        ValueError: if there is no such shader node in the material
    """
    nodes = material.node_tree.nodes

    try:
        node = nodes[name]
        if node.type == type:
            return node
    except KeyError:
        pass

    # Lookup by name failed, let's try the slower way
    for node in nodes:
        if node.type == type:
            return node

    raise KeyError(f"no shader node with type {type!r} in material")


def _get_shader_node_and_input_for_diffuse_color_of_material(material):
    """Returns a reference to the shader node and its input that controls the
    diffuse color of the given material.

    The material must use a principled BSDF or an emission shader.

    Parameters:
        material: the Blender material to update

    Raises:
        ValueError: if the material does not use shader nodes
    """
    try:
        node = _find_shader_node_by_name_and_type(material, "Emission", "EMISSION")
        input = node.inputs["Color"]
        return node, input
    except KeyError:
        try:
            node = _find_shader_node_by_name_and_type(
                material, "Principled BSDF", "BSDF_PRINCIPLED"
            )
            input = node.inputs["Base Color"]
            return node, input
        except KeyError:
            try:
                node = _find_shader_node_by_name_and_type(
                    material, "Principled BSDF", "BSDF_PRINCIPLED"
                )
                input = node.inputs["Emission"]
                return node, input
            except KeyError:
                raise ValueError("Material does not have a diffuse color shader node")


def _get_color(obj, frame):
    """Return diffuse_color of an object at the actual frame.

    Parameters:
        obj: a Blender object
        frame: the current frame

    Return:
        color of object as an R, G, B tuple in [0-255]

    """
    # if there is no material or diffuse color, we return black
    material = obj.active_material
    if not material or not material.diffuse_color:
        return (0, 0, 0)

    # if color is not animated with nodes, use a single color (that can be
    # an animated color as well, which is already evaluated at the given frame)
    if not material.use_nodes:
        return (
            _to_int_255(material.diffuse_color[0]),
            _to_int_255(material.diffuse_color[1]),
            _to_int_255(material.diffuse_color[2]),
        )

    # if a shader node is used, sample it on the given frame
    node, input = _get_shader_node_and_input_for_diffuse_color_of_material(material)
    animation = material.node_tree.animation_data
    # if it is not animated, get the default value
    if not animation:
        rgb = input.default_value[:3]
    # if it is animated, evaluate shader node on the given frame
    else:
        index = node.inputs.find(input.name)
        data_path = f'nodes["{node.name}"].inputs[{index}].default_value'
        rgb = [0, 0, 0]
        for fc in animation.action.fcurves:
            if fc.data_path != data_path:
                continue

            # iterate channels (r, g, b) only
            if fc.array_index not in (0, 1, 2):
                continue

            rgb[fc.array_index] = fc.evaluate(frame)

    return (
        _to_int_255(rgb[0]),
        _to_int_255(rgb[1]),
        _to_int_255(rgb[2]),
    )


def _get_frame_range_from_export_settings(context, settings) -> Tuple[int, int, int]:
    """Get framerange and related variables.

    Parameters:
        context: the main Blender context
        settings: export settings

    Return:
        framerange to be used during the export. Framerange is a 3-tuple
        consisting of (first_frame, last_frame, frame_skip_factor)
    """
    start, end = resolve_frame_range(settings["frame_range"], context=context)
    fps = context.scene.render.fps
    fpsskip = int(fps / settings["output_fps"])
    return start, end, fpsskip


def _get_trajectories_and_lights(
    context, settings, frame_range: Tuple[int, int, int]
) -> Dict[str, List[TimePosColor]]:
    """Get trajectories and lights of all selected/picked objects.

    Parameters:
        context: the main Blender context
        settings: export settings
        framerange: the framerange used for exporting

    Return:
        drone show data in lists of TimePosColor entries, in a dictionary, indexed by object names

    """

    # get object trajectories for each needed frame in convenient format
    fps = context.scene.render.fps
    data = {}
    context.scene.frame_set(frame_range[0])
    objects = list(get_drones_to_export(settings["export_selected"]))

    # initialize trajectories and lights
    for obj in objects:
        data[obj.name] = []

    # parse trajectories and lights
    for frame in range(frame_range[0], frame_range[1] + frame_range[2], frame_range[2]):
        log.debug(f"processing frame {frame}")
        context.scene.frame_set(frame)
        for obj in objects:
            pos = _get_location(obj)
            color = _get_color(obj, frame)
            data[obj.name].append(TimePosColor(int(frame / fps * 1000), *pos, *color))

    return data


def _export_data_to_zip(data_dict: Dict[str, List[TimePosColor]], filepath: Path):
    """Export data to individual csv files zipped into a common file."""
    # write .csv files in a .zip file
    with ZipFile(filepath, "w", ZIP_DEFLATED) as zip_file:
        for name, data in data_dict.items():
            safe_name = re.sub(r"[^A-Za-z0-9\.\+\-]", "_", name)
            lines = [
                ",".join(
                    ["Time [msec]", "x [m]", "y [m]", "z [m]", "Red", "Green", "Blue"]
                )
            ] + [str(item) for item in data]
            zip_file.writestr(safe_name + ".csv", "\n".join(lines))


def _write_skybrush_file(context, settings, filepath: Path) -> None:
    """Creates Skybrush-compatible CSV output from blender trajectories and
    color animation.

    This is a helper function for SkybrushCSVExportOperator

    Parameters:
        context: the main Blender context
        settings: export settings
        filepath: the output path where the export should write

    """

    # get framerange
    log.info("Getting frame range from {}".format(settings["frame_range"]))
    frame_range = _get_frame_range_from_export_settings(context, settings)
    # get trajectories and lights
    log.info("Getting object trajectories and lights")
    trajectories_and_lights = _get_trajectories_and_lights(
        context, settings, frame_range
    )
    # export data to a .zip file containing .csv files
    log.info(f"Exporting object trajectories and light animation to {filepath}")
    _export_data_to_zip(trajectories_and_lights, filepath)

    log.info("Export finished")


class SkybrushCSVExportOperator(Operator, ExportHelper):
    """Export object trajectories and light animation into Skybrush-compatible simple CSV format."""

    bl_idname = "export_scene.skybrush_csv"
    bl_label = "Export Skybrush CSV"
    bl_options = {"REGISTER"}

    # List of file extensions that correspond to Skybrush CSV files (zipped)
    filter_glob = StringProperty(default="*.zip", options={"HIDDEN"})
    filename_ext = ".zip"

    # output all objects or only selected ones
    export_selected = BoolProperty(
        name="Export selected objects only",
        default=True,
        description=(
            "Export only the selected objects from the scene. Uncheck to export "
            "all objects, irrespectively of the selection."
        ),
    )

    # frame range
    frame_range = FrameRangeProperty(default="RENDER")

    # output frame rate
    output_fps = FloatProperty(
        name="Frame rate",
        default=4,
        description="Temporal resolution of exported trajectory and light (frames per second)",
    )

    def execute(self, context):
        filepath = bpy.path.ensure_ext(self.filepath, self.filename_ext)
        settings = {
            "export_selected": self.export_selected,
            "frame_range": self.frame_range,
            "output_fps": self.output_fps,
        }

        if os.path.basename(filepath).lower() == self.filename_ext.lower():
            self.report({"ERROR_INVALID_INPUT"}, "Filename must not be empty")
            return {"CANCELLED"}

        objects = list(get_drones_to_export(self.export_selected))
        if not objects:
            if self.export_selected:
                self.report({"WARNING"}, "No objects were selected; export cancelled")
            else:
                self.report(
                    {"WARNING"}, "There are no objects to export; export cancelled"
                )
            return {"CANCELLED"}

        _write_skybrush_file(context, settings, filepath)

        return {"FINISHED"}

    def invoke(self, context, event):
        if not self.filepath:
            filepath = bpy.data.filepath or "Untitled"
            filepath, _ = os.path.splitext(filepath)
            self.filepath = f"{filepath}.zip"

        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}
