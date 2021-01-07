import bgl
import blf
import bpy
import gpu

from gpu_extras.batch import batch_for_shader
from typing import List, Sequence

from sbstudio.model.types import Coordinate3D

from .base import Overlay

__all__ = ("SafetyCheckOverlay",)

#: Type specification for markers on the overlay. Each marker is a sequence of
#: coordinates that are interconnected with lines.
MarkerList = List[Sequence[Coordinate3D]]


def set_warning_color_iff(condition: bool, font_id: int) -> None:
    if condition:
        blf.color(font_id, 1, 1, 0, 1)
    else:
        blf.color(font_id, 1, 1, 1, 1)


class SafetyCheckOverlay(Overlay):
    """Overlay that marks the closest pair of drones and all drones above the
    altitude threshold in the 3D view.
    """

    def __init__(self):
        super().__init__()

        self._markers = None
        self._pixel_size = 1
        self._shader_batches = None

    @property
    def markers(self):
        return self._markers

    @markers.setter
    def markers(self, value):
        if value is not None:
            self._markers = []
            for marker_points in value:
                marker_points = tuple(
                    tuple(float(coord) for coord in point) for point in marker_points
                )
                self._markers.append(marker_points)

        else:
            self._markers = None

        self._shader_batches = None

    def prepare(self) -> None:
        self._shader = gpu.shader.from_builtin("3D_UNIFORM_COLOR")
        self._pixel_size = bpy.context.preferences.system.pixel_size

    def draw_2d(self) -> None:
        skybrush = getattr(bpy.context.scene, "skybrush", None)
        safety_check = getattr(skybrush, "safety_check", None)
        if not safety_check:
            return

        font_id = 0

        context = bpy.context

        left_panel_width = context.area.regions[2].width
        total_height = context.area.height

        left_margin = left_panel_width + 19 * self._pixel_size
        y = total_height - 112 * self._pixel_size
        line_height = 20 * self._pixel_size

        blf.size(font_id, int(11 * self._pixel_size), 72)
        blf.enable(font_id, blf.SHADOW)

        if safety_check.min_distance_is_valid:
            set_warning_color_iff(safety_check.should_show_proximity_warning, font_id)
            blf.position(font_id, left_margin, y, 0)
            blf.draw(font_id, f"Min distance: {safety_check.min_distance:.1f} m")
            y -= line_height

        if safety_check.max_altitude_is_valid:
            set_warning_color_iff(safety_check.should_show_altitude_warning, font_id)
            blf.position(font_id, left_margin, y, 0)
            blf.draw(font_id, f"Max altitude: {safety_check.max_altitude:.1f} m")
            y -= line_height

        if safety_check.max_velocities_are_valid:
            set_warning_color_iff(safety_check.should_show_velocity_warning, font_id)
            blf.position(font_id, left_margin, y, 0)
            blf.draw(
                font_id,
                f"Max velocity XY: {safety_check.max_velocity_xy:.1f} m/s | Z: {safety_check.max_velocity_z:.1f} m/s",
            )
            y -= line_height

    def draw_3d(self) -> None:
        bgl.glEnable(bgl.GL_BLEND)

        if self._markers is not None:
            if self._shader_batches is None:
                self._shader_batches = self._create_shader_batches()

            if self._shader_batches:
                self._shader.bind()
                self._shader.uniform_float("color", (1, 0, 0, 1))
                bgl.glLineWidth(5)
                bgl.glPointSize(20)
                for batch in self._shader_batches:
                    batch.draw(self._shader)

    def dispose(self) -> None:
        self._shader = None
        self._shader_batches = None

    def _create_shader_batches(self) -> None:
        batches, points, lines = [], [], []

        for marker_points in self._markers:
            points.extend(marker_points)

            if marker_points:
                if len(marker_points) > 2:
                    prev = points[-1]
                    for curr in marker_points:
                        lines.extend((prev, curr))
                        prev = curr
                elif len(marker_points) == 2:
                    lines.extend(marker_points)

        # Construct the shader batch to draw the lines on the UI
        batches.extend(
            [
                batch_for_shader(self._shader, "LINES", {"pos": lines}),
                batch_for_shader(self._shader, "POINTS", {"pos": points}),
            ]
        )

        return batches
