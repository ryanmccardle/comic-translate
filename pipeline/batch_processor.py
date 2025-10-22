import os
import json
import shutil
import requests
import logging
import traceback
import imkit as imk
import time
from datetime import datetime
from typing import List
from PySide6.QtGui import QColor

from modules.detection.processor import TextBlockDetector
from modules.detection.utils.content import get_inpaint_bboxes
from modules.translation.processor import Translator
from modules.utils.textblock import sort_blk_list
from modules.utils.pipeline_utils import inpaint_map, get_config, generate_mask, get_language_code, is_directory_empty
from modules.utils.translator_utils import get_raw_translation, get_raw_text, format_translations
from modules.utils.archives import make
from modules.rendering.render import get_best_render_area, pyside_word_wrap
from modules.utils.device import resolve_device
from app.ui.canvas.text_item import OutlineInfo, OutlineType
from app.ui.canvas.text.text_item_properties import TextItemProperties
from app.ui.canvas.save_renderer import ImageSaveRenderer


logger = logging.getLogger(__name__)


class BatchProcessor:
    """Handles batch processing of comic translation."""

    STAGE_DETECT = "detect"
    STAGE_OCR = "ocr"
    STAGE_TRANSLATE = "translate"
    STAGE_SEGMENT = "segment"
    STAGE_CLEAN = "clean"
    STAGE_RENDER = "render"
    STAGES_IN_ORDER = [
        STAGE_DETECT,
        STAGE_OCR,
        STAGE_SEGMENT,
        STAGE_CLEAN,
        STAGE_TRANSLATE,
        STAGE_RENDER,
    ]

    def __init__(
            self,
            main_page,
            cache_manager,
            block_detection_handler,
            inpainting_handler, 
            ocr_handler
        ):
        
        self.main_page = main_page
        self.cache_manager = cache_manager
        # Use shared handlers from the main pipeline
        self.block_detection = block_detection_handler
        self.inpainting = inpainting_handler
        self.ocr_handler = ocr_handler

    # ------------------------------------------------------------------
    # Helper utilities shared across the legacy full batch pipeline and
    # the new stage-based batch operations.
    # ------------------------------------------------------------------

    def _ensure_image_state_defaults(self, image_path: str) -> dict:
        state = self.main_page.image_states.setdefault(image_path, {})
        state.setdefault('viewer_state', {})
        state.setdefault('source_lang', self.main_page.s_combo.currentText())
        state.setdefault('target_lang', self.main_page.t_combo.currentText())
        state.setdefault('brush_strokes', [])
        state.setdefault('blk_list', [])
        state.setdefault('skip', False)
        return state

    def _get_archive_directory(self, image_path: str) -> tuple[str, str]:
        directory = os.path.dirname(image_path)
        archive_bname = ""
        for archive in self.main_page.file_handler.archive_info:
            images = archive['extracted_images']
            archive_path = archive['archive_path']
            for img_pth in images:
                if img_pth == image_path:
                    directory = os.path.dirname(archive_path)
                    archive_bname = os.path.splitext(os.path.basename(archive_path))[0]
                    return directory, archive_bname
        return directory, archive_bname

    def _build_processing_context(self, image_path: str, timestamp: str | None) -> dict:
        settings_page = self.main_page.settings_page
        state = self._ensure_image_state_defaults(image_path)
        source_lang = state.get('source_lang', self.main_page.s_combo.currentText())
        target_lang = state.get('target_lang', self.main_page.t_combo.currentText())
        base_name = os.path.splitext(os.path.basename(image_path))[0]
        extension = os.path.splitext(image_path)[1]
        directory, archive_bname = self._get_archive_directory(image_path)
        image = imk.read_image(image_path)
        file_on_display = None
        if self.main_page.image_files and 0 <= self.main_page.curr_img_idx < len(self.main_page.image_files):
            file_on_display = self.main_page.image_files[self.main_page.curr_img_idx]

        target_lang_en = self.main_page.lang_mapping.get(target_lang, target_lang)
        trg_lng_cd = get_language_code(target_lang_en)

        return {
            'image_path': image_path,
            'image': image,
            'state': state,
            'source_lang': source_lang,
            'target_lang': target_lang,
            'settings_page': settings_page,
            'base_name': base_name,
            'extension': extension,
            'directory': directory,
            'archive_bname': archive_bname,
            'timestamp': timestamp,
            'file_on_display': file_on_display,
            'target_lang_en': target_lang_en,
            'trg_lng_cd': trg_lng_cd,
        }

    def _should_cancel(self) -> bool:
        if self.main_page.current_worker and self.main_page.current_worker.is_cancelled:
            self.main_page.current_worker = None
            return True
        return False

    def _store_blk_list(self, image_path: str, blk_list: list) -> None:
        state = self._ensure_image_state_defaults(image_path)
        state['blk_list'] = blk_list
        if (self.main_page.image_files and
                0 <= self.main_page.curr_img_idx < len(self.main_page.image_files) and
                self.main_page.image_files[self.main_page.curr_img_idx] == image_path):
            self.main_page.blk_list = blk_list

    def _detect_blocks_for_context(self, context: dict) -> list:
        image = context['image']
        if image is None:
            return []
        settings_page = context['settings_page']
        if self.block_detection.block_detector_cache is None:
            self.block_detection.block_detector_cache = TextBlockDetector(settings_page)
        blk_list = self.block_detection.block_detector_cache.detect(image)
        if blk_list:
            source_lang = context['source_lang']
            source_lang_english = self.main_page.lang_mapping.get(source_lang, source_lang)
            rtl = True if source_lang_english == 'Japanese' else False
            blk_list = sort_blk_list(blk_list, rtl)
        return blk_list

    def _ocr_blocks_for_context(self, context: dict, blk_list: list) -> list:
        image = context['image']
        if image is None or not blk_list:
            return blk_list
        settings_page = context['settings_page']
        source_lang = context['source_lang']
        ocr_model = settings_page.get_tool_selection('ocr')
        device = resolve_device(settings_page.is_gpu_enabled())
        newline_mode = settings_page.get_ocr_newline_mode()
        cache_key = self.cache_manager._get_ocr_cache_key(image, source_lang, ocr_model, device, newline_mode)
        self.ocr_handler.ocr.initialize(self.main_page, source_lang)
        processed_blocks = self.ocr_handler.ocr.process(image, blk_list)
        if processed_blocks is not None:
            blk_list = processed_blocks
        self.cache_manager._cache_ocr_results(cache_key, blk_list)
        source_lang_english = self.main_page.lang_mapping.get(source_lang, source_lang)
        rtl = True if source_lang_english == 'Japanese' else False
        return sort_blk_list(blk_list, rtl)

    def _translate_blocks_for_context(self, context: dict, blk_list: list) -> list:
        if not blk_list:
            return blk_list
        settings_page = context['settings_page']
        image = context['image']
        source_lang = context['source_lang']
        target_lang = context['target_lang']
        translator_key = settings_page.get_tool_selection('translator')
        translator = Translator(self.main_page, source_lang, target_lang)
        extra_context = settings_page.get_llm_settings()['extra_context']
        translation_cache_key = self.cache_manager._get_translation_cache_key(
            image, source_lang, target_lang, translator_key, extra_context
        )
        translator.translate(blk_list, image, extra_context)
        self.cache_manager._cache_translation_results(translation_cache_key, blk_list)
        return blk_list

    def _export_text_outputs(self, context: dict, blk_list: list) -> None:
        settings_page = context['settings_page']
        export_settings = settings_page.get_export_settings()
        timestamp = context['timestamp']
        if not timestamp:
            return
        directory = context['directory']
        archive_bname = context['archive_bname']
        image_path = context['image_path']
        if export_settings['export_raw_text']:
            entire_raw_text = get_raw_text(blk_list)
            path = os.path.join(directory, f"comic_translate_{timestamp}", "raw_texts", archive_bname)
            if not os.path.exists(path):
                os.makedirs(path, exist_ok=True)
            with open(
                os.path.join(path, os.path.splitext(os.path.basename(image_path))[0] + "_raw.txt"),
                'w', encoding='UTF-8'
            ) as file:
                file.write(entire_raw_text)

        if export_settings['export_translated_text']:
            entire_translated_text = get_raw_translation(blk_list)
            path = os.path.join(directory, f"comic_translate_{timestamp}", "translated_texts", archive_bname)
            if not os.path.exists(path):
                os.makedirs(path, exist_ok=True)
            with open(
                os.path.join(path, os.path.splitext(os.path.basename(image_path))[0] + "_translated.txt"),
                'w', encoding='UTF-8'
            ) as file:
                file.write(entire_translated_text)

    def _ensure_inpainter(self, settings_page) -> None:
        if (self.inpainting.inpainter_cache is None or
                self.inpainting.cached_inpainter_key != settings_page.get_tool_selection('inpainter')):
            device = resolve_device(settings_page.is_gpu_enabled())
            inpainter_key = settings_page.get_tool_selection('inpainter')
            InpainterClass = inpaint_map[inpainter_key]
            logger.info("pre-inpaint: initializing inpainter '%s' on device %s", inpainter_key, device)
            t0 = time.time()
            self.inpainting.inpainter_cache = InpainterClass(device, backend='onnx')
            self.inpainting.cached_inpainter_key = inpainter_key
            t1 = time.time()
            logger.info("pre-inpaint: inpainter initialized in %.2fs", t1 - t0)

    def _single_stage_progress(self, index: int, total: int, step: int, steps: int, change_name: bool) -> None:
        self.main_page.progress_update.emit(index, total, step, steps, change_name)

    def skip_save(self, directory, timestamp, base_name, extension, archive_bname, image):
        path = os.path.join(directory, f"comic_translate_{timestamp}", "translated_images", archive_bname)
        if not os.path.exists(path):
            os.makedirs(path, exist_ok=True)
        imk.write_image(os.path.join(path, f"{base_name}_translated{extension}"), image)

    def emit_progress(self, index, total, step, steps, change_name):
        """Wrapper around main_page.progress_update.emit that logs a human-readable stage."""
        stage_map = {
            0: 'start-image',
            1: 'text-block-detection',
            2: 'ocr-processing',
            3: 'pre-inpaint-setup',
            4: 'generate-mask',
            5: 'inpainting',
            7: 'translation',
            9: 'text-rendering-prepare',
            10: 'save-and-finish',
        }
        stage_name = stage_map.get(step, f'stage-{step}')
        logger.info(f"Progress: image_index={index}/{total} step={step}/{steps} ({stage_name}) change_name={change_name}")
        self.main_page.progress_update.emit(index, total, step, steps, change_name)

    # ------------------------------------------------------------------
    # Stage-based batch execution
    # ------------------------------------------------------------------

    def run_stage(self, stage_name: str, selected_paths: List[str] | None = None):
        if stage_name not in self.STAGES_IN_ORDER:
            raise ValueError(f"Unknown batch stage '{stage_name}'")
        self.run_stages([stage_name], selected_paths)

    def run_stages(self, stages: List[str], selected_paths: List[str] | None = None):
        stage_sequence = [stage for stage in self.STAGES_IN_ORDER if stage in stages]
        if not stage_sequence:
            return

        image_list = selected_paths if selected_paths is not None else list(self.main_page.image_files)
        total_images = len(image_list)
        if total_images == 0:
            return

        timestamp_needed = any(stage in {self.STAGE_TRANSLATE, self.STAGE_CLEAN, self.STAGE_RENDER}
                               for stage in stage_sequence)
        timestamp = datetime.now().strftime("%b-%d-%Y_%I-%M-%S%p") if timestamp_needed else None

        for stage_name in stage_sequence:
            if stage_name == self.STAGE_DETECT:
                self._run_detect_stage(image_list, timestamp, total_images)
            elif stage_name == self.STAGE_OCR:
                self._run_ocr_stage(image_list, timestamp, total_images)
            elif stage_name == self.STAGE_SEGMENT:
                self._run_segment_stage(image_list, timestamp, total_images)
            elif stage_name == self.STAGE_CLEAN:
                self._run_clean_stage(image_list, timestamp, total_images)
            elif stage_name == self.STAGE_TRANSLATE:
                self._run_translate_stage(image_list, timestamp, total_images)
            elif stage_name == self.STAGE_RENDER:
                self._run_render_stage(image_list, timestamp, total_images)

    # ------------------------------------------------------------------
    # Individual stage implementations
    # ------------------------------------------------------------------

    def _run_detect_stage(self, image_list: List[str], timestamp: str | None, total_images: int) -> None:
        for index, image_path in enumerate(image_list):
            self._single_stage_progress(index, total_images, 0, 1, True)
            if self._should_cancel():
                break
            context = self._build_processing_context(image_path, timestamp)
            state = context['state']
            if context['state'].get('skip', False):
                continue
            blk_list = self._detect_blocks_for_context(context)
            if not blk_list:
                self._store_blk_list(image_path, [])
                viewer_state = state.get('viewer_state', {})
                if isinstance(viewer_state, dict):
                    viewer_state.pop('rectangles', None)
                    viewer_state.pop('text_items_state', None)
                self.main_page.image_skipped.emit(image_path, "Text Blocks", "")
                continue
            self._store_blk_list(image_path, blk_list)
            self._single_stage_progress(index, total_images, 1, 1, False)

    def _run_ocr_stage(self, image_list: List[str], timestamp: str | None, total_images: int) -> None:
        for index, image_path in enumerate(image_list):
            self._single_stage_progress(index, total_images, 0, 1, True)
            if self._should_cancel():
                break
            context = self._build_processing_context(image_path, timestamp)
            if context['state'].get('skip', False):
                continue
            blk_list = context['state'].get('blk_list', [])
            if not blk_list:
                blk_list = self._detect_blocks_for_context(context)
                if not blk_list:
                    self.main_page.image_skipped.emit(image_path, "Text Blocks", "")
                    continue
            try:
                blk_list = self._ocr_blocks_for_context(context, blk_list)
            except Exception as e:
                err_msg = str(e)
                if isinstance(e, requests.exceptions.HTTPError):
                    try:
                        err_json = e.response.json()
                        err_msg = err_json.get("error_description", err_msg)
                    except Exception:
                        pass
                logger.exception(f"OCR processing failed: {err_msg}")
                self.main_page.image_skipped.emit(image_path, "OCR", err_msg)
                continue
            self._store_blk_list(image_path, blk_list)
            self._single_stage_progress(index, total_images, 1, 1, False)

    def _run_segment_stage(self, image_list: List[str], timestamp: str | None, total_images: int) -> None:
        for index, image_path in enumerate(image_list):
            self._single_stage_progress(index, total_images, 0, 1, True)
            if self._should_cancel():
                break
            context = self._build_processing_context(image_path, timestamp)
            if context['state'].get('skip', False):
                continue
            blk_list = context['state'].get('blk_list', [])
            if not blk_list:
                blk_list = self._detect_blocks_for_context(context)
                if not blk_list:
                    self.main_page.image_skipped.emit(image_path, "Text Blocks", "")
                    continue
            image = context['image']
            if image is None:
                continue
            for blk in blk_list:
                try:
                    xyxy = getattr(blk, 'xyxy', None)
                    if xyxy is None:
                        blk.inpaint_bboxes = None
                        continue
                    blk.inpaint_bboxes = get_inpaint_bboxes(xyxy, image)
                except Exception:
                    blk.inpaint_bboxes = None
            self._store_blk_list(image_path, blk_list)
            self._single_stage_progress(index, total_images, 1, 1, False)

    def _run_clean_stage(self, image_list: List[str], timestamp: str | None, total_images: int) -> None:
        for index, image_path in enumerate(image_list):
            self._single_stage_progress(index, total_images, 0, 1, True)
            if self._should_cancel():
                break
            context = self._build_processing_context(image_path, timestamp)
            state = context['state']
            if state.get('skip', False):
                continue
            blk_list = state.get('blk_list', [])
            if not blk_list:
                blk_list = self._detect_blocks_for_context(context)
                if not blk_list:
                    self.main_page.image_skipped.emit(image_path, "Text Blocks", "")
                    continue
            image = context['image']
            if image is None:
                continue
            settings_page = context['settings_page']
            self._ensure_inpainter(settings_page)
            config = get_config(settings_page)
            mask = generate_mask(image, blk_list)
            inpaint_input_img = self.inpainting.inpainter_cache(image, mask, config)
            inpaint_input_img = imk.convert_scale_abs(inpaint_input_img)
            state['clean_image'] = inpaint_input_img
            patches = self.inpainting.get_inpainted_patches(mask, inpaint_input_img)
            self.main_page.patches_processed.emit(patches, image_path)
            export_settings = settings_page.get_export_settings()
            if export_settings['export_inpainted_image'] and timestamp:
                path = os.path.join(context['directory'], f"comic_translate_{timestamp}", "cleaned_images", context['archive_bname'])
                if not os.path.exists(path):
                    os.makedirs(path, exist_ok=True)
                imk.write_image(os.path.join(path, f"{context['base_name']}_cleaned{context['extension']}"), inpaint_input_img)
            self._single_stage_progress(index, total_images, 1, 1, False)

    def _run_translate_stage(self, image_list: List[str], timestamp: str | None, total_images: int) -> None:
        for index, image_path in enumerate(image_list):
            self._single_stage_progress(index, total_images, 0, 1, True)
            if self._should_cancel():
                break
            context = self._build_processing_context(image_path, timestamp)
            state = context['state']
            if state.get('skip', False):
                continue
            blk_list = state.get('blk_list', [])
            if not blk_list:
                blk_list = self._detect_blocks_for_context(context)
                if not blk_list:
                    self.main_page.image_skipped.emit(image_path, "Text Blocks", "")
                    continue
            needs_ocr = any(not getattr(blk, 'text', '').strip() for blk in blk_list)
            if needs_ocr:
                try:
                    blk_list = self._ocr_blocks_for_context(context, blk_list)
                except Exception as e:
                    err_msg = str(e)
                    if isinstance(e, requests.exceptions.HTTPError):
                        try:
                            err_json = e.response.json()
                            err_msg = err_json.get("error_description", err_msg)
                        except Exception:
                            pass
                    logger.exception(f"OCR processing failed: {err_msg}")
                    self.main_page.image_skipped.emit(image_path, "OCR", err_msg)
                    continue
            try:
                blk_list = self._translate_blocks_for_context(context, blk_list)
            except Exception as e:
                err_msg = str(e)
                if isinstance(e, requests.exceptions.HTTPError):
                    try:
                        err_json = e.response.json()
                        err_msg = err_json.get("error_description", err_msg)
                    except Exception:
                        pass
                logger.exception(f"Translation failed: {err_msg}")
                self.main_page.image_skipped.emit(image_path, "Translator", err_msg)
                continue
            self._export_text_outputs(context, blk_list)
            self._store_blk_list(image_path, blk_list)
            self._single_stage_progress(index, total_images, 1, 1, False)

    def _run_render_stage(self, image_list: List[str], timestamp: str | None, total_images: int) -> None:
        for index, image_path in enumerate(image_list):
            self._single_stage_progress(index, total_images, 0, 1, True)
            if self._should_cancel():
                break
            context = self._build_processing_context(image_path, timestamp)
            state = context['state']
            if state.get('skip', False):
                continue
            blk_list = state.get('blk_list', [])
            if not blk_list:
                blk_list = self._detect_blocks_for_context(context)
                if not blk_list:
                    self.main_page.image_skipped.emit(image_path, "Text Blocks", "")
                    continue
            needs_translation = any(not getattr(blk, 'translation', '').strip() for blk in blk_list)
            if needs_translation:
                needs_ocr = any(not getattr(blk, 'text', '').strip() for blk in blk_list)
                if needs_ocr:
                    try:
                        blk_list = self._ocr_blocks_for_context(context, blk_list)
                    except Exception as e:
                        err_msg = str(e)
                        if isinstance(e, requests.exceptions.HTTPError):
                            try:
                                err_json = e.response.json()
                                err_msg = err_json.get("error_description", err_msg)
                            except Exception:
                                pass
                        logger.exception(f"OCR processing failed: {err_msg}")
                        self.main_page.image_skipped.emit(image_path, "OCR", err_msg)
                        continue
                try:
                    blk_list = self._translate_blocks_for_context(context, blk_list)
                except Exception as e:
                    err_msg = str(e)
                    if isinstance(e, requests.exceptions.HTTPError):
                        try:
                            err_json = e.response.json()
                            err_msg = err_json.get("error_description", err_msg)
                        except Exception:
                            pass
                    logger.exception(f"Translation failed: {err_msg}")
                    self.main_page.image_skipped.emit(image_path, "Translator", err_msg)
                    continue
                self._export_text_outputs(context, blk_list)

            render_settings = self.main_page.render_settings()
            format_translations(blk_list, context['trg_lng_cd'], upper_case=render_settings.upper_case)
            clean_image = state.get('clean_image')
            image = context['image']
            get_best_render_area(blk_list, image, clean_image)

            font = render_settings.font_family
            font_color = QColor(render_settings.color)
            max_font_size = render_settings.max_font_size
            min_font_size = render_settings.min_font_size
            line_spacing = float(render_settings.line_spacing)
            outline_width = float(render_settings.outline_width)
            outline_color = QColor(render_settings.outline_color)
            bold = render_settings.bold
            italic = render_settings.italic
            underline = render_settings.underline
            alignment_id = render_settings.alignment_id
            alignment = self.main_page.button_to_alignment[alignment_id]
            direction = render_settings.direction
            outline = render_settings.outline

            text_items_state = []
            for blk in blk_list:
                x1, y1, width, height = blk.xywh
                translation = getattr(blk, 'translation', '')
                if not translation or len(translation) == 1:
                    continue
                translation, font_size = pyside_word_wrap(
                    translation, font, width, height,
                    line_spacing, outline_width, bold, italic, underline,
                    alignment, direction, max_font_size, min_font_size
                )
                if image_path == context['file_on_display']:
                    self.main_page.blk_rendered.emit(translation, font_size, blk)
                trg_lng_cd = context['trg_lng_cd'] or ''
                if any(lang in trg_lng_cd.lower() for lang in ['zh', 'ja', 'th']):
                    translation = translation.replace(' ', '')
                text_props = TextItemProperties(
                    text=translation,
                    font_family=font,
                    font_size=font_size,
                    text_color=font_color,
                    alignment=alignment,
                    line_spacing=line_spacing,
                    outline_color=outline_color,
                    outline_width=outline_width,
                    bold=bold,
                    italic=italic,
                    underline=underline,
                    position=(x1, y1),
                    rotation=blk.angle,
                    scale=1.0,
                    transform_origin=blk.tr_origin_point,
                    width=width,
                    direction=direction,
                    selection_outlines=[
                        OutlineInfo(0, len(translation), outline_color, outline_width, OutlineType.Full_Document)
                    ] if outline else [],
                )
                text_items_state.append(text_props.to_dict())

            viewer_state = state.setdefault('viewer_state', {})
            viewer_state['text_items_state'] = text_items_state
            viewer_state['push_to_stack'] = True
            self._store_blk_list(image_path, blk_list)

            render_save_dir = os.path.join(
                context['directory'],
                f"comic_translate_{timestamp}" if timestamp else "comic_translate",
                "translated_images",
                context['archive_bname']
            )
            if timestamp:
                if not os.path.exists(render_save_dir):
                    os.makedirs(render_save_dir, exist_ok=True)
                sv_pth = os.path.join(render_save_dir, f"{context['base_name']}_translated{context['extension']}")
                renderer = ImageSaveRenderer(image)
                patches = self.main_page.image_patches.get(image_path, [])
                renderer.apply_patches(patches)
                renderer.add_state_to_image(viewer_state.copy())
                renderer.save_image(sv_pth)

            self._single_stage_progress(index, total_images, 1, 1, False)
    def log_skipped_image(self, directory, timestamp, image_path, reason="", full_traceback=""):
        skipped_file = os.path.join(directory, f"comic_translate_{timestamp}", "skipped_images.txt")
        with open(skipped_file, 'a', encoding='UTF-8') as file:
            file.write(image_path + "\n")
            file.write(reason + "\n")
            if full_traceback:
                file.write("Full Traceback:\n")
                file.write(full_traceback + "\n")
            file.write("\n")

    def batch_process(self, selected_paths: List[str] = None):
        timestamp = datetime.now().strftime("%b-%d-%Y_%I-%M-%S%p")
        image_list = selected_paths if selected_paths is not None else self.main_page.image_files
        total_images = len(image_list)

        for index, image_path in enumerate(image_list):

            file_on_display = self.main_page.image_files[self.main_page.curr_img_idx]

            # index, step, total_steps, change_name
            self.emit_progress(index, total_images, 0, 10, True)

            settings_page = self.main_page.settings_page
            source_lang = self.main_page.image_states[image_path]['source_lang']
            target_lang = self.main_page.image_states[image_path]['target_lang']

            target_lang_en = self.main_page.lang_mapping.get(target_lang, None)
            trg_lng_cd = get_language_code(target_lang_en)
            
            base_name = os.path.splitext(os.path.basename(image_path))[0]
            extension = os.path.splitext(image_path)[1]
            directory = os.path.dirname(image_path)

            archive_bname = ""
            for archive in self.main_page.file_handler.archive_info:
                images = archive['extracted_images']
                archive_path = archive['archive_path']

                for img_pth in images:
                    if img_pth == image_path:
                        directory = os.path.dirname(archive_path)
                        archive_bname = os.path.splitext(os.path.basename(archive_path))[0]

            image = imk.read_image(image_path)

            # skip UI-skipped images
            state = self.main_page.image_states.get(image_path, {})
            if state.get('skip', False):
                self.skip_save(directory, timestamp, base_name, extension, archive_bname, image)
                self.log_skipped_image(directory, timestamp, image_path, "User-skipped")
                continue

            # Text Block Detection
            self.emit_progress(index, total_images, 1, 10, False)
            if self.main_page.current_worker and self.main_page.current_worker.is_cancelled:
                self.main_page.current_worker = None
                break

            # Use the shared block detector from the handler
            if self.block_detection.block_detector_cache is None:
                self.block_detection.block_detector_cache = TextBlockDetector(settings_page)
            
            blk_list = self.block_detection.block_detector_cache.detect(image)

            self.emit_progress(index, total_images, 2, 10, False)
            if self.main_page.current_worker and self.main_page.current_worker.is_cancelled:
                self.main_page.current_worker = None
                break

            if blk_list:
                # Get ocr cache key for batch processing
                ocr_model = settings_page.get_tool_selection('ocr')
                device = resolve_device(settings_page.is_gpu_enabled())
                newline_mode = settings_page.get_ocr_newline_mode()
                cache_key = self.cache_manager._get_ocr_cache_key(image, source_lang, ocr_model, device, newline_mode)
                # Use the shared OCR processor from the handler
                self.ocr_handler.ocr.initialize(self.main_page, source_lang)
                try:
                    self.ocr_handler.ocr.process(image, blk_list)
                    # Cache the OCR results for potential future use
                    self.cache_manager._cache_ocr_results(cache_key, self.main_page.blk_list)
                    source_lang_english = self.main_page.lang_mapping.get(source_lang, source_lang)
                    rtl = True if source_lang_english == 'Japanese' else False
                    blk_list = sort_blk_list(blk_list, rtl)
                    
                except Exception as e:
                    # if it's an HTTPError, try to pull the "error_description" field
                    if isinstance(e, requests.exceptions.HTTPError):
                        try:
                            err_json = e.response.json()
                            err_msg = err_json.get("error_description", str(e))
                        except Exception:
                            err_msg = str(e)
                    else:
                        err_msg = str(e)

                    logger.exception(f"OCR processing failed: {err_msg}")
                    reason = f"OCR: {err_msg}"
                    full_traceback = traceback.format_exc()
                    self.skip_save(directory, timestamp, base_name, extension, archive_bname, image)
                    self.main_page.image_skipped.emit(image_path, "OCR", err_msg)
                    self.log_skipped_image(directory, timestamp, image_path, reason, full_traceback)
                    continue
            else:
                self.skip_save(directory, timestamp, base_name, extension, archive_bname, image)
                self.main_page.image_skipped.emit(image_path, "Text Blocks", "")
                self.log_skipped_image(directory, timestamp, image_path, "No text blocks detected")
                continue

            self.emit_progress(index, total_images, 3, 10, False)
            if self.main_page.current_worker and self.main_page.current_worker.is_cancelled:
                self.main_page.current_worker = None
                break

            # Clean Image of text
            export_settings = settings_page.get_export_settings()

            # Use the shared inpainter from the handler
            if self.inpainting.inpainter_cache is None or self.inpainting.cached_inpainter_key != settings_page.get_tool_selection('inpainter'):
                device = resolve_device(settings_page.is_gpu_enabled())
                inpainter_key = settings_page.get_tool_selection('inpainter')
                InpainterClass = inpaint_map[inpainter_key]
                logger.info("pre-inpaint: initializing inpainter '%s' on device %s", inpainter_key, device)
                t0 = time.time()
                self.inpainting.inpainter_cache = InpainterClass(device, backend='onnx')
                self.inpainting.cached_inpainter_key = inpainter_key
                t1 = time.time()
                logger.info("pre-inpaint: inpainter initialized in %.2fs", t1 - t0)

            config = get_config(settings_page)
            logger.info("pre-inpaint: generating mask (blk_list=%d blocks)", len(blk_list))
            t0 = time.time()
            mask = generate_mask(image, blk_list)
            t1 = time.time()
            logger.info("pre-inpaint: mask generated in %.2fs (mask shape=%s)", t1 - t0, getattr(mask, 'shape', None))

            self.emit_progress(index, total_images, 4, 10, False)
            if self.main_page.current_worker and self.main_page.current_worker.is_cancelled:
                self.main_page.current_worker = None
                break

            inpaint_input_img = self.inpainting.inpainter_cache(image, mask, config)
            inpaint_input_img = imk.convert_scale_abs(inpaint_input_img)

            # Saving cleaned image
            patches = self.inpainting.get_inpainted_patches(mask, inpaint_input_img)
            self.main_page.patches_processed.emit(patches, image_path)

            # inpaint_input_img is already in RGB format

            if export_settings['export_inpainted_image']:
                path = os.path.join(directory, f"comic_translate_{timestamp}", "cleaned_images", archive_bname)
                if not os.path.exists(path):
                    os.makedirs(path, exist_ok=True)
                imk.write_image(os.path.join(path, f"{base_name}_cleaned{extension}"), inpaint_input_img)

            self.emit_progress(index, total_images, 5, 10, False)
            if self.main_page.current_worker and self.main_page.current_worker.is_cancelled:
                self.main_page.current_worker = None
                break

            # Get Translations/ Export if selected
            extra_context = settings_page.get_llm_settings()['extra_context']
            translator_key = settings_page.get_tool_selection('translator')
            translator = Translator(self.main_page, source_lang, target_lang)
            
            # Get translation cache key for batch processing
            translation_cache_key = self.cache_manager._get_translation_cache_key(
                image, source_lang, target_lang, translator_key, extra_context
            )
            
            try:
                translator.translate(blk_list, image, extra_context)
                # Cache the translation results for potential future use
                self.cache_manager._cache_translation_results(translation_cache_key, blk_list)
            except Exception as e:
                # if it's an HTTPError, try to pull the "error_description" field
                if isinstance(e, requests.exceptions.HTTPError):
                    try:
                        err_json = e.response.json()
                        err_msg = err_json.get("error_description", str(e))
                    except Exception:
                        err_msg = str(e)
                else:
                    err_msg = str(e)

                logger.exception(f"Translation failed: {err_msg}")
                reason = f"Translator: {err_msg}"
                full_traceback = traceback.format_exc()
                self.skip_save(directory, timestamp, base_name, extension, archive_bname, image)
                self.main_page.image_skipped.emit(image_path, "Translator", err_msg)
                self.log_skipped_image(directory, timestamp, image_path, reason, full_traceback)
                continue

            entire_raw_text = get_raw_text(blk_list)
            entire_translated_text = get_raw_translation(blk_list)

            # Parse JSON strings and check if they're empty objects or invalid
            try:
                raw_text_obj = json.loads(entire_raw_text)
                translated_text_obj = json.loads(entire_translated_text)
                
                if (not raw_text_obj) or (not translated_text_obj):
                    self.skip_save(directory, timestamp, base_name, extension, archive_bname, image)
                    self.main_page.image_skipped.emit(image_path, "Translator", "")
                    self.log_skipped_image(directory, timestamp, image_path, "Translator: empty JSON")
                    continue
            except json.JSONDecodeError as e:
                # Handle invalid JSON
                error_message = str(e)
                reason = f"Translator: JSONDecodeError: {error_message}"
                logger.exception(reason)
                full_traceback = traceback.format_exc()
                self.skip_save(directory, timestamp, base_name, extension, archive_bname, image)
                self.main_page.image_skipped.emit(image_path, "Translator", error_message)
                self.log_skipped_image(directory, timestamp, image_path, reason, full_traceback)
                continue

            if export_settings['export_raw_text']:
                path = os.path.join(directory, f"comic_translate_{timestamp}", "raw_texts", archive_bname)
                if not os.path.exists(path):
                    os.makedirs(path, exist_ok=True)
                file = open(os.path.join(path, os.path.splitext(os.path.basename(image_path))[0] + "_raw.txt"), 'w', encoding='UTF-8')
                file.write(entire_raw_text)

            if export_settings['export_translated_text']:
                path = os.path.join(directory, f"comic_translate_{timestamp}", "translated_texts", archive_bname)
                if not os.path.exists(path):
                    os.makedirs(path, exist_ok=True)
                file = open(os.path.join(path, os.path.splitext(os.path.basename(image_path))[0] + "_translated.txt"), 'w', encoding='UTF-8')
                file.write(entire_translated_text)

            self.emit_progress(index, total_images, 7, 10, False)
            if self.main_page.current_worker and self.main_page.current_worker.is_cancelled:
                self.main_page.current_worker = None
                break

            # Text Rendering
            render_settings = self.main_page.render_settings()
            upper_case = render_settings.upper_case
            outline = render_settings.outline
            format_translations(blk_list, trg_lng_cd, upper_case=upper_case)
            get_best_render_area(blk_list, image, inpaint_input_img)

            font = render_settings.font_family
            font_color = QColor(render_settings.color)

            max_font_size = render_settings.max_font_size
            min_font_size = render_settings.min_font_size
            line_spacing = float(render_settings.line_spacing) 
            outline_width = float(render_settings.outline_width)
            outline_color = QColor(render_settings.outline_color) 
            bold = render_settings.bold
            italic = render_settings.italic
            underline = render_settings.underline
            alignment_id = render_settings.alignment_id
            alignment = self.main_page.button_to_alignment[alignment_id]
            direction = render_settings.direction
                
            text_items_state = []
            for blk in blk_list:
                x1, y1, width, height = blk.xywh

                translation = blk.translation
                if not translation or len(translation) == 1:
                    continue

                translation, font_size = pyside_word_wrap(translation, font, width, height,
                                                        line_spacing, outline_width, bold, italic, underline,
                                                        alignment, direction, max_font_size, min_font_size)
                
                # Display text if on current page  
                if image_path == file_on_display:
                    self.main_page.blk_rendered.emit(translation, font_size, blk)

                # Language-specific formatting for state storage
                if any(lang in trg_lng_cd.lower() for lang in ['zh', 'ja', 'th']):
                    translation = translation.replace(' ', '')

                # Use TextItemProperties for consistent text item creation
                text_props = TextItemProperties(
                    text=translation,
                    font_family=font,
                    font_size=font_size,
                    text_color=font_color,
                    alignment=alignment,
                    line_spacing=line_spacing,
                    outline_color=outline_color,
                    outline_width=outline_width,
                    bold=bold,
                    italic=italic,
                    underline=underline,
                    position=(x1, y1),
                    rotation=blk.angle,
                    scale=1.0,
                    transform_origin=blk.tr_origin_point,
                    width=width,
                    direction=direction,
                    selection_outlines=[
                        OutlineInfo(0, len(translation), 
                        outline_color, 
                        outline_width, 
                        OutlineType.Full_Document)
                    ] if outline else [],
                )
                text_items_state.append(text_props.to_dict())

            self.main_page.image_states[image_path]['viewer_state'].update({
                'text_items_state': text_items_state
                })
            
            self.main_page.image_states[image_path]['viewer_state'].update({
                'push_to_stack': True
                })
            
            self.emit_progress(index, total_images, 9, 10, False)
            if self.main_page.current_worker and self.main_page.current_worker.is_cancelled:
                self.main_page.current_worker = None
                break

            # Saving blocks with texts to history
            self.main_page.image_states[image_path].update({
                'blk_list': blk_list                   
            })

            if image_path == file_on_display:
                self.main_page.blk_list = blk_list
                
            render_save_dir = os.path.join(directory, f"comic_translate_{timestamp}", "translated_images", archive_bname)
            if not os.path.exists(render_save_dir):
                os.makedirs(render_save_dir, exist_ok=True)
            sv_pth = os.path.join(render_save_dir, f"{base_name}_translated{extension}")

            renderer = ImageSaveRenderer(image)
            viewer_state = self.main_page.image_states[image_path]['viewer_state'].copy()
            patches = self.main_page.image_patches.get(image_path, [])
            renderer.apply_patches(patches)
            renderer.add_state_to_image(viewer_state)
            renderer.save_image(sv_pth)

            self.emit_progress(index, total_images, 10, 10, False)

        archive_info_list = self.main_page.file_handler.archive_info
        if archive_info_list:
            save_as_settings = settings_page.get_export_settings()['save_as']
            for archive_index, archive in enumerate(archive_info_list):
                archive_index_input = total_images + archive_index

                self.emit_progress(archive_index_input, total_images, 1, 3, True)
                if self.main_page.current_worker and self.main_page.current_worker.is_cancelled:
                    self.main_page.current_worker = None
                    break

                archive_path = archive['archive_path']
                archive_ext = os.path.splitext(archive_path)[1]
                archive_bname = os.path.splitext(os.path.basename(archive_path))[0]
                archive_directory = os.path.dirname(archive_path)
                save_as_ext = f".{save_as_settings[archive_ext.lower()]}"

                save_dir = os.path.join(archive_directory, f"comic_translate_{timestamp}", "translated_images", archive_bname)
                check_from = os.path.join(archive_directory, f"comic_translate_{timestamp}")

                self.emit_progress(archive_index_input, total_images, 2, 3, True)
                if self.main_page.current_worker and self.main_page.current_worker.is_cancelled:
                    self.main_page.current_worker = None
                    break

                # Create the new archive
                output_base_name = f"{archive_bname}"
                make(save_as_ext=save_as_ext, input_dir=save_dir, 
                    output_dir=archive_directory, output_base_name=output_base_name)

                self.emit_progress(archive_index_input, total_images, 3, 3, True)
                if self.main_page.current_worker and self.main_page.current_worker.is_cancelled:
                    self.main_page.current_worker = None
                    break

                # Clean up temporary 
                if os.path.exists(save_dir):
                    shutil.rmtree(save_dir)
                # The temp dir is removed when closing the app

                if is_directory_empty(check_from):
                    shutil.rmtree(check_from)
