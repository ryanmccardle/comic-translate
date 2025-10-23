from __future__ import annotations

import copy
import numpy as np
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from PySide6 import QtCore
from PySide6.QtGui import QColor, QTextDocument

from app.ui.commands.textformat import TextFormatCommand
from app.ui.commands.box import AddTextItemCommand
from app.ui.canvas.text_item import TextBlockItem, OutlineInfo, OutlineType
from app.ui.canvas.text.text_item_properties import TextItemProperties

from modules.utils.textblock import TextBlock
from modules.rendering.render import TextRenderingSettings, manual_wrap
from modules.utils.pipeline_utils import font_selected, get_language_code, \
    get_layout_direction, is_close
from modules.utils.translator_utils import format_translations

if TYPE_CHECKING:
    from controller import ComicTranslate


@dataclass
class FontSettings:
    font_family: str
    font_size: float
    line_spacing: float
    text_color: QColor
    text_color_hex: str
    alignment: QtCore.Qt.AlignmentFlag
    bold: bool
    italic: bool
    underline: bool
    outline_enabled: bool
    outline_color: Optional[QColor]
    outline_color_hex: Optional[str]
    outline_width: float
    direction: QtCore.Qt.LayoutDirection

class TextController:
    def __init__(self, main: ComicTranslate):
        self.main = main

        # List of widgets to block signals for during manual rendering
        self.widgets_to_block = [
            self.main.font_dropdown,
            self.main.font_size_dropdown,
            self.main.line_spacing_dropdown,
            self.main.block_font_color_button,
            self.main.outline_font_color_button,
            self.main.outline_width_dropdown,
            self.main.outline_checkbox
        ]

    def connect_text_item_signals(self, text_item: TextBlockItem):
        text_item.item_selected.connect(self.on_text_item_selected)
        text_item.item_deselected.connect(self.on_text_item_deselected)
        text_item.text_changed.connect(self.update_text_block_from_item)
        text_item.text_highlighted.connect(self.set_values_from_highlight)
        text_item.change_undo.connect(self.main.rect_item_ctrl.rect_change_undo)

    def clear_text_edits(self):
        self.main.curr_tblock = None
        self.main.curr_tblock_item = None
        self.main.s_text_edit.clear()
        self.main.t_text_edit.clear()

    def on_blk_rendered(self, text: str, font_size: int, blk: TextBlock):
        if not self.main.image_viewer.hasPhoto():
            print("No main image to add to.")
            return

        target_lang = self.main.lang_mapping.get(self.main.t_combo.currentText(), None)
        trg_lng_cd = get_language_code(target_lang)
        if any(lang in trg_lng_cd.lower() for lang in ['zh', 'ja', 'th']):
            text = text.replace(' ', '')

        render_settings = self.render_settings()
        font_family = render_settings.font_family
        text_color_str = render_settings.color
        text_color = QColor(text_color_str)

        id = render_settings.alignment_id
        alignment = self.main.button_to_alignment[id]
        line_spacing = float(render_settings.line_spacing)
        outline_color_str = render_settings.outline_color
        outline_color = QColor(outline_color_str) if self.main.outline_checkbox.isChecked() else None
        outline_width = float(render_settings.outline_width)
        bold = render_settings.bold
        italic = render_settings.italic
        underline = render_settings.underline
        direction = render_settings.direction

        block_width = max(1, int(blk.xyxy[2] - blk.xyxy[0]))

        properties = TextItemProperties(
            text=text,
            font_family=font_family,
            font_size=font_size,
            text_color=text_color,
            alignment=alignment,
            line_spacing=line_spacing,
            outline_color=outline_color,
            outline_width=outline_width,
            bold=bold,
            italic=italic,
            underline=underline,
            direction=direction,
            position=(blk.xyxy[0], blk.xyxy[1]),
            rotation=blk.angle,
            width=block_width,
        )

        text_item = self.main.image_viewer.add_text_item(properties)
        text_item.set_plain_text(text)
        text_item.setTextWidth(block_width)

        command = AddTextItemCommand(self.main, text_item)
        self.main.push_command(command)

    def on_text_item_selected(self, text_item: TextBlockItem):
        self.main.curr_tblock_item = text_item

        x1, y1 = int(text_item.pos().x()), int(text_item.pos().y())
        rotation = text_item.rotation()

        self.main.curr_tblock = next(
            (
            blk for blk in self.main.blk_list
            if is_close(blk.xyxy[0], x1, 5) and is_close(blk.xyxy[1], y1, 5)
            and is_close(blk.angle, rotation, 1)
            ),
            None
        )

        # Update both s_text_edit and t_text_edit
        if self.main.curr_tblock:
            self.main.s_text_edit.blockSignals(True)
            self.main.s_text_edit.setPlainText(self.main.curr_tblock.text)
            self.main.s_text_edit.blockSignals(False)

        self.main.t_text_edit.blockSignals(True)
        self.main.t_text_edit.setPlainText(text_item.toPlainText())
        self.main.t_text_edit.blockSignals(False)

        self.set_values_for_blk_item(text_item)

    def on_text_item_deselected(self):
        self.clear_text_edits()

    def update_text_block(self):
        if self.main.curr_tblock:
            self.main.curr_tblock.text = self.main.s_text_edit.toPlainText()
            self.main.curr_tblock.translation = self.main.t_text_edit.toPlainText()

    def update_text_block_from_edit(self):
        new_text = self.main.t_text_edit.toPlainText()
        if self.main.curr_tblock:
            self.main.curr_tblock.translation = new_text

        if self.main.curr_tblock_item and self.main.curr_tblock_item in self.main.image_viewer._scene.items():
            cursor_position = self.main.t_text_edit.textCursor().position()
            self.main.curr_tblock_item.setPlainText(new_text)

            # Restore cursor position
            cursor = self.main.t_text_edit.textCursor()
            cursor.setPosition(cursor_position)
            self.main.t_text_edit.setTextCursor(cursor)

    def update_text_block_from_item(self, new_text: str):
        if self.main.curr_tblock and new_text:
            self.main.curr_tblock.translation = new_text
            self.main.t_text_edit.blockSignals(True)
            self.main.t_text_edit.setPlainText(new_text)
            self.main.t_text_edit.blockSignals(False)

    def save_src_trg(self):
        source_lang = self.main.s_combo.currentText()
        target_lang = self.main.t_combo.currentText()
        
        if self.main.curr_img_idx >= 0:
            current_file = self.main.image_files[self.main.curr_img_idx]
            self.main.image_states[current_file]['source_lang'] = source_lang
            self.main.image_states[current_file]['target_lang'] = target_lang

        target_en = self.main.lang_mapping.get(target_lang, None)
        t_direction = get_layout_direction(target_en)
        t_text_option = self.main.t_text_edit.document().defaultTextOption()
        t_text_option.setTextDirection(t_direction)
        self.main.t_text_edit.document().setDefaultTextOption(t_text_option)

    def set_src_trg_all(self):
        source_lang = self.main.s_combo.currentText()
        target_lang = self.main.t_combo.currentText()
        for image_path in self.main.image_files:
            self.main.image_states[image_path]['source_lang'] = source_lang
            self.main.image_states[image_path]['target_lang'] = target_lang

    def change_all_blocks_size(self, diff: int):
        if len(self.main.blk_list) == 0:
            return
        updated_blk_list = []
        for blk in self.main.blk_list:
            blk_rect = tuple(blk.xyxy)
            blk.xyxy[:] = [blk_rect[0] - diff, blk_rect[1] - diff, blk_rect[2] + diff, blk_rect[3] + diff]
            updated_blk_list.append(blk)
        self.main.blk_list = updated_blk_list
        self.main.pipeline.load_box_coords(self.main.blk_list)

    # Formatting actions
    def on_font_dropdown_change(self, font_family: str):
        if self.main.curr_tblock_item and font_family:
            old_item = copy.copy(self.main.curr_tblock_item)
            font_size = int(self.main.font_size_dropdown.currentText())
            self.main.curr_tblock_item.set_font(font_family, font_size)

            command = TextFormatCommand(self.main.image_viewer, old_item, self.main.curr_tblock_item)
            self.main.push_command(command)

    def on_font_size_change(self, font_size: str):
        if self.main.curr_tblock_item and font_size:
            old_item = copy.copy(self.main.curr_tblock_item)
            font_size = float(font_size)
            self.main.curr_tblock_item.set_font_size(font_size)

            command = TextFormatCommand(self.main.image_viewer, old_item, self.main.curr_tblock_item)
            self.main.push_command(command)

    def on_line_spacing_change(self, line_spacing: str):
        if self.main.curr_tblock_item and line_spacing:
            old_item = copy.copy(self.main.curr_tblock_item)
            spacing = float(line_spacing)
            self.main.curr_tblock_item.set_line_spacing(spacing)

            command = TextFormatCommand(self.main.image_viewer, old_item, self.main.curr_tblock_item)
            self.main.push_command(command)

    def on_font_color_change(self):
        font_color = self.main.get_color()
        if font_color and font_color.isValid():
            self.main.block_font_color_button.setStyleSheet(
                f"background-color: {font_color.name()}; border: none; border-radius: 5px;"
            )
            self.main.block_font_color_button.setProperty('selected_color', font_color.name())
            if self.main.curr_tblock_item:
                old_item = copy.copy(self.main.curr_tblock_item)
                self.main.curr_tblock_item.set_color(font_color)

                command = TextFormatCommand(self.main.image_viewer, old_item, self.main.curr_tblock_item)
                self.main.push_command(command)

    def left_align(self):
        if self.main.curr_tblock_item:
            old_item = copy.copy(self.main.curr_tblock_item)
            self.main.curr_tblock_item.set_alignment(QtCore.Qt.AlignmentFlag.AlignLeft)

            command = TextFormatCommand(self.main.image_viewer, old_item, self.main.curr_tblock_item)
            self.main.push_command(command)

    def center_align(self):
        if self.main.curr_tblock_item:
            old_item = copy.copy(self.main.curr_tblock_item)
            self.main.curr_tblock_item.set_alignment(QtCore.Qt.AlignmentFlag.AlignCenter)

            command = TextFormatCommand(self.main.image_viewer, old_item, self.main.curr_tblock_item)
            self.main.push_command(command)

    def right_align(self):
        if self.main.curr_tblock_item:
            old_item = copy.copy(self.main.curr_tblock_item)
            self.main.curr_tblock_item.set_alignment(QtCore.Qt.AlignmentFlag.AlignRight)

            command = TextFormatCommand(self.main.image_viewer, old_item, self.main.curr_tblock_item)
            self.main.push_command(command)

    def bold(self):
        if self.main.curr_tblock_item:
            old_item = copy.copy(self.main.curr_tblock_item)
            state = self.main.bold_button.isChecked()
            self.main.curr_tblock_item.set_bold(state)

            command = TextFormatCommand(self.main.image_viewer, old_item, self.main.curr_tblock_item)
            self.main.push_command(command)

    def italic(self):
        if self.main.curr_tblock_item:
            old_item = copy.copy(self.main.curr_tblock_item)
            state = self.main.italic_button.isChecked()
            self.main.curr_tblock_item.set_italic(state)

            command = TextFormatCommand(self.main.image_viewer, old_item, self.main.curr_tblock_item)
            self.main.push_command(command)

    def underline(self):
        if self.main.curr_tblock_item:
            old_item = copy.copy(self.main.curr_tblock_item)
            state = self.main.underline_button.isChecked()
            self.main.curr_tblock_item.set_underline(state)

            command = TextFormatCommand(self.main.image_viewer, old_item, self.main.curr_tblock_item)
            self.main.push_command(command)

    def on_outline_color_change(self):
        outline_color = self.main.get_color()
        if outline_color and outline_color.isValid():
            self.main.outline_font_color_button.setStyleSheet(
                f"background-color: {outline_color.name()}; border: none; border-radius: 5px;"
            )
            self.main.outline_font_color_button.setProperty('selected_color', outline_color.name())
            outline_width = float(self.main.outline_width_dropdown.currentText())

            if self.main.curr_tblock_item and self.main.outline_checkbox.isChecked():
                old_item = copy.copy(self.main.curr_tblock_item)
                self.main.curr_tblock_item.set_outline(outline_color, outline_width)

                command = TextFormatCommand(self.main.image_viewer, old_item, self.main.curr_tblock_item)
                self.main.push_command(command)

    def on_outline_width_change(self, outline_width):
        if self.main.curr_tblock_item and self.main.outline_checkbox.isChecked():
            old_item = copy.copy(self.main.curr_tblock_item)
            outline_width = float(self.main.outline_width_dropdown.currentText())
            color_str = self.main.outline_font_color_button.property('selected_color')
            color = QColor(color_str)
            self.main.curr_tblock_item.set_outline(color, outline_width)

            command = TextFormatCommand(self.main.image_viewer, old_item, self.main.curr_tblock_item)
            self.main.push_command(command)

    def toggle_outline_settings(self, state):
        enabled = True if state == 2 else False
        if self.main.curr_tblock_item:
            if not enabled:
                self.main.curr_tblock_item.set_outline(None, None)
            else:
                old_item = copy.copy(self.main.curr_tblock_item)
                outline_width = float(self.main.outline_width_dropdown.currentText())
                color_str = self.main.outline_font_color_button.property('selected_color')
                color = QColor(color_str)
                self.main.curr_tblock_item.set_outline(color, outline_width)

                command = TextFormatCommand(self.main.image_viewer, old_item, self.main.curr_tblock_item)
                self.main.push_command(command)

    # Bulk font application helpers
    def apply_font_to_page(self):
        settings = self._get_current_font_settings()
        items = self._get_text_items_for_current_page()
        if not items:
            return

        self._apply_font_settings_to_items(items, settings, self.main.tr("Apply font settings to page"))
        self._update_state_for_page(settings)

    def apply_font_to_project(self):
        settings = self._get_current_font_settings()
        items = list(self.main.image_viewer.text_items)

        if items:
            self._apply_font_settings_to_items(items, settings, self.main.tr("Apply font settings to project"))

        self._update_state_for_page(settings)
        self._update_all_states(settings)

    def _get_current_font_settings(self) -> FontSettings:
        render_settings = self.render_settings()

        font_size_text = self.main.font_size_dropdown.currentText()
        try:
            font_size = float(font_size_text)
        except (TypeError, ValueError):
            font_size = float(render_settings.max_font_size)

        try:
            line_spacing = float(render_settings.line_spacing)
        except (TypeError, ValueError):
            line_spacing = 1.0

        text_color = QColor(render_settings.color or '#000000')
        outline_enabled = self.main.outline_checkbox.isChecked()

        outline_color = QColor(render_settings.outline_color) if outline_enabled else None

        try:
            outline_width = float(render_settings.outline_width)
        except (TypeError, ValueError):
            outline_width = 1.0

        alignment = self.main.button_to_alignment.get(render_settings.alignment_id, QtCore.Qt.AlignmentFlag.AlignLeft)

        return FontSettings(
            font_family=render_settings.font_family,
            font_size=font_size,
            line_spacing=line_spacing,
            text_color=text_color,
            text_color_hex=text_color.name(),
            alignment=alignment,
            bold=render_settings.bold,
            italic=render_settings.italic,
            underline=render_settings.underline,
            outline_enabled=outline_enabled,
            outline_color=outline_color,
            outline_color_hex=outline_color.name() if outline_color else None,
            outline_width=outline_width,
            direction=render_settings.direction,
        )

    def _get_text_items_for_current_page(self) -> list[TextBlockItem]:
        if not self.main.webtoon_mode:
            return list(self.main.image_viewer.text_items)

        page_idx = self.main.curr_img_idx
        manager = getattr(self.main.image_viewer, 'webtoon_manager', None)
        if manager is None or page_idx is None or page_idx < 0:
            return []

        positions = getattr(manager, 'image_positions', [])
        if not positions or page_idx >= len(positions):
            return list(self.main.image_viewer.text_items)

        page_y_start = positions[page_idx]
        heights = getattr(manager, 'image_heights', [])
        if page_idx < len(positions) - 1:
            page_y_end = positions[page_idx + 1]
        else:
            page_height = heights[page_idx] if page_idx < len(heights) else 0
            page_y_end = page_y_start + page_height

        results = []
        for item in self.main.image_viewer.text_items:
            pos_y = item.pos().y()
            if page_y_start <= pos_y < page_y_end:
                results.append(item)

        return results

    def _apply_font_settings_to_items(self, items: list[TextBlockItem], settings: FontSettings, macro_name: str):
        if not items:
            return

        undo_stack = self.main.undo_group.activeStack()
        if undo_stack is not None:
            undo_stack.beginMacro(macro_name)

        try:
            for item in items:
                old_item = copy.copy(item)

                item.set_font(settings.font_family, settings.font_size)
                item.set_font_size(settings.font_size)
                item.set_line_spacing(settings.line_spacing)
                item.set_color(settings.text_color)
                item.set_alignment(settings.alignment)
                item.set_bold(settings.bold)
                item.set_italic(settings.italic)
                item.set_underline(settings.underline)
                item.set_direction(settings.direction)

                if settings.outline_enabled and settings.outline_color is not None:
                    item.set_outline(settings.outline_color, settings.outline_width)
                else:
                    item.set_outline(None, None)

                command = TextFormatCommand(self.main.image_viewer, old_item, item)
                self.main.push_command(command)

                self._update_textblock_style(item, settings)
        finally:
            if undo_stack is not None:
                undo_stack.endMacro()

    def _update_textblock_style(self, item: TextBlockItem, settings: FontSettings):
        x1, y1 = int(item.pos().x()), int(item.pos().y())
        rotation = item.rotation()

        blk = next(
            (
                blk for blk in self.main.blk_list
                if is_close(blk.xyxy[0], x1, 5)
                and is_close(blk.xyxy[1], y1, 5)
                and is_close(blk.angle, rotation, 1)
            ),
            None,
        )

        if blk:
            blk.line_spacing = settings.line_spacing
            blk.alignment = self._alignment_to_string(settings.alignment)
            blk.font_color = settings.text_color_hex

    @staticmethod
    def _alignment_to_string(alignment: QtCore.Qt.AlignmentFlag) -> str:
        if alignment == QtCore.Qt.AlignmentFlag.AlignRight:
            return 'right'
        if alignment == QtCore.Qt.AlignmentFlag.AlignCenter:
            return 'center'
        return 'left'

    def _calculate_outline_span(self, text: str) -> int:
        doc = QTextDocument()
        if text:
            doc.setHtml(text)
        return max(0, doc.characterCount() - 1)

    def _build_outline_objects(self, text: str, settings: FontSettings) -> list[OutlineInfo]:
        if not settings.outline_enabled or not settings.outline_color:
            return []

        end = self._calculate_outline_span(text)
        return [
            OutlineInfo(
                start=0,
                end=end,
                color=settings.outline_color,
                width=settings.outline_width,
                type=OutlineType.Full_Document,
            )
        ]

    def _apply_settings_to_text_states(self, text_states, settings: FontSettings):
        if not text_states:
            return

        for idx, text_state in enumerate(text_states):
            if hasattr(text_state, 'font_family'):
                text_state.font_family = settings.font_family
                text_state.font_size = settings.font_size
                text_state.line_spacing = settings.line_spacing
                text_state.bold = settings.bold
                text_state.italic = settings.italic
                text_state.underline = settings.underline
                text_state.alignment = settings.alignment
                text_state.direction = settings.direction
                text_state.text_color = settings.text_color
                text_state.outline_width = settings.outline_width if settings.outline_enabled else 0
                text_state.outline_color = settings.outline_color if settings.outline_enabled else None
                text_state.selection_outlines = self._build_outline_objects(text_state.text, settings)
            elif isinstance(text_state, dict):
                text_state['font_family'] = settings.font_family
                text_state['font_size'] = settings.font_size
                text_state['line_spacing'] = settings.line_spacing
                text_state['bold'] = settings.bold
                text_state['italic'] = settings.italic
                text_state['underline'] = settings.underline
                text_state['alignment'] = int(settings.alignment)
                text_state['direction'] = settings.direction
                text_state['text_color'] = settings.text_color
                text_state['outline_width'] = settings.outline_width if settings.outline_enabled else 0
                text_state['outline_color'] = settings.outline_color if settings.outline_enabled else None
                text_state['selection_outlines'] = self._build_outline_objects(text_state.get('text', ''), settings)
                text_states[idx] = text_state

    def _update_saved_blk_list_styles(self, blocks, settings: FontSettings):
        if not blocks:
            return

        alignment_str = self._alignment_to_string(settings.alignment)

        for blk in blocks:
            if hasattr(blk, 'line_spacing'):
                blk.line_spacing = settings.line_spacing
                blk.alignment = alignment_str
                blk.font_color = settings.text_color_hex
            elif isinstance(blk, dict):
                blk['line_spacing'] = settings.line_spacing
                blk['alignment'] = alignment_str
                blk['font_color'] = settings.text_color_hex

    def _update_state_for_page(self, settings: FontSettings, page_idx: Optional[int] = None):
        if page_idx is None:
            page_idx = self.main.curr_img_idx

        if page_idx is None or page_idx < 0 or page_idx >= len(self.main.image_files):
            return

        file_path = self.main.image_files[page_idx]
        state = self.main.image_states.get(file_path)
        if state is None:
            return

        if not self.main.webtoon_mode:
            viewer_state = self.main.image_viewer.save_state()
            self._apply_settings_to_text_states(viewer_state.get('text_items_state', []), settings)
            state['viewer_state'] = viewer_state
            state['brush_strokes'] = self.main.image_viewer.save_brush_strokes()
            state['source_lang'] = self.main.s_combo.currentText()
            state['target_lang'] = self.main.t_combo.currentText()
            state['blk_list'] = self.main.blk_list.copy()
        else:
            viewer_state = state.setdefault('viewer_state', {})
            page_state = self.main.project_ctrl._create_text_items_state_from_scene(page_idx)
            text_states = page_state.get('text_items_state', [])
            self._apply_settings_to_text_states(text_states, settings)
            viewer_state['text_items_state'] = text_states
            self._update_saved_blk_list_styles(state.get('blk_list', []), settings)

    def _update_all_states(self, settings: FontSettings):
        current_idx = self.main.curr_img_idx

        for idx, file_path in enumerate(self.main.image_files):
            state = self.main.image_states.get(file_path)
            if state is None or idx == current_idx:
                continue

            self._update_saved_blk_list_styles(state.get('blk_list', []), settings)

            viewer_state = state.get('viewer_state')
            if viewer_state:
                self._apply_settings_to_text_states(viewer_state.get('text_items_state', []), settings)

    # Widget helpers
    def block_text_item_widgets(self, widgets):
        # Block signals
        for widget in widgets:
            widget.blockSignals(True)

        # Block Signals is buggy for these, so use disconnect/connect
        self.main.bold_button.clicked.disconnect(self.bold)
        self.main.italic_button.clicked.disconnect(self.italic)
        self.main.underline_button.clicked.disconnect(self.underline)

        self.main.alignment_tool_group.get_button_group().buttons()[0].clicked.disconnect(self.left_align)
        self.main.alignment_tool_group.get_button_group().buttons()[1].clicked.disconnect(self.center_align)
        self.main.alignment_tool_group.get_button_group().buttons()[2].clicked.disconnect(self.right_align)

    def unblock_text_item_widgets(self, widgets):
        # Unblock signals
        for widget in widgets:
            widget.blockSignals(False)

        self.main.bold_button.clicked.connect(self.bold)
        self.main.italic_button.clicked.connect(self.italic)
        self.main.underline_button.clicked.connect(self.underline)

        self.main.alignment_tool_group.get_button_group().buttons()[0].clicked.connect(self.left_align)
        self.main.alignment_tool_group.get_button_group().buttons()[1].clicked.connect(self.center_align)
        self.main.alignment_tool_group.get_button_group().buttons()[2].clicked.connect(self.right_align)

    def set_values_for_blk_item(self, text_item: TextBlockItem):

        self.block_text_item_widgets(self.widgets_to_block)

        try:
            # Set values
            self.main.font_dropdown.setCurrentText(text_item.font_family)
            self.main.font_size_dropdown.setCurrentText(str(int(text_item.font_size)))

            self.main.line_spacing_dropdown.setCurrentText(str(text_item.line_spacing))

            self.main.block_font_color_button.setStyleSheet(
                f"background-color: {text_item.text_color.name()}; border: none; border-radius: 5px;"
            )
            self.main.block_font_color_button.setProperty('selected_color', text_item.text_color.name())

            if text_item.outline_color is not None:
                self.main.outline_font_color_button.setStyleSheet(
                    f"background-color: {text_item.outline_color.name()}; border: none; border-radius: 5px;"
                )
                self.main.outline_font_color_button.setProperty('selected_color', text_item.outline_color.name())
            else:
                self.main.outline_font_color_button.setStyleSheet(
                    "background-color: white; border: none; border-radius: 5px;"
                )
                self.main.outline_font_color_button.setProperty('selected_color', '#ffffff')

            self.main.outline_width_dropdown.setCurrentText(str(text_item.outline_width))
            self.main.outline_checkbox.setChecked(text_item.outline)

            self.main.bold_button.setChecked(text_item.bold)
            self.main.italic_button.setChecked(text_item.italic)
            self.main.underline_button.setChecked(text_item.underline)

            alignment_to_button = {
                QtCore.Qt.AlignmentFlag.AlignLeft: 0,
                QtCore.Qt.AlignmentFlag.AlignCenter: 1,
                QtCore.Qt.AlignmentFlag.AlignRight: 2,
            }

            alignment = text_item.alignment
            button_group = self.main.alignment_tool_group.get_button_group()

            if alignment in alignment_to_button:
                button_index = alignment_to_button[alignment]
                button_group.buttons()[button_index].setChecked(True)

        finally:
            self.unblock_text_item_widgets(self.widgets_to_block)

    def set_values_from_highlight(self, item_highlighted = None):

        self.block_text_item_widgets(self.widgets_to_block)

        # Attributes
        font_family = item_highlighted['font_family']
        font_size = item_highlighted['font_size']
        text_color =  item_highlighted['text_color']

        outline_color = item_highlighted['outline_color']
        outline_width =  item_highlighted['outline_width']
        outline = item_highlighted['outline']

        bold = item_highlighted['bold']
        italic =  item_highlighted['italic']
        underline = item_highlighted['underline']

        alignment = item_highlighted['alignment']

        try:
            # Set values
            self.main.font_dropdown.setCurrentText(font_family) if font_family else None
            self.main.font_size_dropdown.setCurrentText(str(int(font_size))) if font_size else None

            if text_color is not None:
                self.main.block_font_color_button.setStyleSheet(
                    f"background-color: {text_color}; border: none; border-radius: 5px;"
                )
                self.main.block_font_color_button.setProperty('selected_color', text_color)

            if outline_color is not None:
                self.main.outline_font_color_button.setStyleSheet(
                    f"background-color: {outline_color}; border: none; border-radius: 5px;"
                )
                self.main.outline_font_color_button.setProperty('selected_color', outline_color)
            else:
                self.main.outline_font_color_button.setStyleSheet(
                    "background-color: white; border: none; border-radius: 5px;"
                )
                self.main.outline_font_color_button.setProperty('selected_color', '#ffffff')

            self.main.outline_width_dropdown.setCurrentText(str(outline_width)) if outline_width else None
            self.main.outline_checkbox.setChecked(outline)

            self.main.bold_button.setChecked(bold)
            self.main.italic_button.setChecked(italic)
            self.main.underline_button.setChecked(underline)

            alignment_to_button = {
                QtCore.Qt.AlignmentFlag.AlignLeft: 0,
                QtCore.Qt.AlignmentFlag.AlignCenter: 1,
                QtCore.Qt.AlignmentFlag.AlignRight: 2,
            }

            button_group = self.main.alignment_tool_group.get_button_group()

            if alignment in alignment_to_button:
                button_index = alignment_to_button[alignment]
                button_group.buttons()[button_index].setChecked(True)

        finally:
            self.unblock_text_item_widgets(self.widgets_to_block)

    # Rendering
    def render_text(self):
        if self.main.image_viewer.hasPhoto() and self.main.blk_list:
            self.main.set_tool(None)
            if not font_selected(self.main):
                return
            self.clear_text_edits()
            self.main.loading.setVisible(True)
            self.main.disable_hbutton_group()

            # Add items to the scene if they're not already present
            for item in self.main.image_viewer.text_items:
                if item not in self.main.image_viewer._scene.items():
                    self.main.image_viewer._scene.addItem(item)

            # Create a dictionary to map text items to their positions and rotations
            existing_text_items = {item: (int(item.pos().x()), int(item.pos().y()), item.rotation()) for item in self.main.image_viewer.text_items}

            # Identify new blocks based on position and rotation
            new_blocks = [
                blk for blk in self.main.blk_list
                if (int(blk.xyxy[0]), int(blk.xyxy[1]), blk.angle) not in existing_text_items.values()
            ]

            self.main.image_viewer.clear_rectangles()
            self.main.curr_tblock = None
            self.main.curr_tblock_item = None

            render_settings = self.render_settings()
            upper = render_settings.upper_case

            line_spacing = float(self.main.line_spacing_dropdown.currentText())
            font_family = self.main.font_dropdown.currentText()
            outline_width = float(self.main.outline_width_dropdown.currentText())

            bold = self.main.bold_button.isChecked()
            italic = self.main.italic_button.isChecked()
            underline = self.main.underline_button.isChecked()

            target_lang = self.main.t_combo.currentText()
            target_lang_en = self.main.lang_mapping.get(target_lang, None)
            trg_lng_cd = get_language_code(target_lang_en)

            self.main.run_threaded(
            lambda: format_translations(self.main.blk_list, trg_lng_cd, upper_case=upper)
            )

            min_font_size = self.main.settings_page.get_min_font_size()
            max_font_size = self.main.settings_page.get_max_font_size()

            align_id = self.main.alignment_tool_group.get_dayu_checked()
            alignment = self.main.button_to_alignment[align_id]
            direction = render_settings.direction

            self.main.undo_group.activeStack().beginMacro('text_items_rendered')
            self.main.run_threaded(manual_wrap, self.on_render_complete, self.main.default_error_handler,
                              None, self.main, new_blocks, font_family, line_spacing, outline_width,
                              bold, italic, underline, alignment, direction, max_font_size,
                              min_font_size)

    def on_render_complete(self, rendered_image: np.ndarray):
        # self.main.set_image(rendered_image) 
        self.main.loading.setVisible(False)
        self.main.enable_hbutton_group()
        self.main.undo_group.activeStack().endMacro()

    def render_settings(self) -> TextRenderingSettings:
        target_lang = self.main.lang_mapping.get(self.main.t_combo.currentText(), None)
        direction = get_layout_direction(target_lang)

        return TextRenderingSettings(
            alignment_id = self.main.alignment_tool_group.get_dayu_checked(),
            font_family = self.main.font_dropdown.currentText(),
            min_font_size = int(self.main.settings_page.ui.min_font_spinbox.value()),
            max_font_size = int(self.main.settings_page.ui.max_font_spinbox.value()),
            color = self.main.block_font_color_button.property('selected_color'),
            upper_case = self.main.settings_page.ui.uppercase_checkbox.isChecked(),
            outline = self.main.outline_checkbox.isChecked(),
            outline_color = self.main.outline_font_color_button.property('selected_color'),
            outline_width = self.main.outline_width_dropdown.currentText(),
            bold = self.main.bold_button.isChecked(),
            italic = self.main.italic_button.isChecked(),
            underline = self.main.underline_button.isChecked(),
            line_spacing = self.main.line_spacing_dropdown.currentText(),
            direction = direction
        )