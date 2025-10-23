import os
import threading
from collections import deque
from typing import Set

from PySide6 import QtCore
from PySide6.QtCore import QTimer, QThread, QObject, Signal, QSize, QMetaObject, Qt
from PySide6.QtGui import QPixmap, QImage, QImageReader
from PySide6.QtWidgets import QListWidget


class ImageLoadWorker(QObject):
    """Worker thread for loading images in the background."""

    image_loaded = Signal(int, QImage)  # index, image

    def __init__(self):
        super().__init__()
        self.load_queue: deque[tuple[int, str, QSize]] = deque()
        self.pending_indices: Set[int] = set()
        self.should_stop = False
        self._queue_lock = threading.Lock()
        self._processing = False

    def add_to_queue(self, index: int, file_path: str, target_size: QSize, priority: bool = False):
        """Add an image to the loading queue."""
        if self.should_stop:
            return

        start_processing = False
        with self._queue_lock:
            if index in self.pending_indices:
                return
            self.pending_indices.add(index)
            # Store a copy of the target size so future updates don't mutate queued jobs
            job = (index, file_path, QSize(target_size))
            if priority:
                self.load_queue.appendleft(job)
            else:
                self.load_queue.append(job)
            if not self._processing:
                self._processing = True
                start_processing = True

        if start_processing:
            QMetaObject.invokeMethod(self, "_process_queue", Qt.QueuedConnection)

    def clear_queue(self):
        """Clear the loading queue."""
        with self._queue_lock:
            self.load_queue.clear()
            self.pending_indices.clear()

    def stop(self):
        """Stop the worker."""
        self.should_stop = True
        self.clear_queue()

    @QtCore.Slot()
    def _process_queue(self):
        while True:
            with self._queue_lock:
                if not self.load_queue or self.should_stop:
                    self._processing = False
                    break
                index, file_path, target_size = self.load_queue.popleft()
                self.pending_indices.discard(index)

            image = self._load_and_resize_image(file_path, target_size)
            if not image.isNull():
                self.image_loaded.emit(index, image)

    def _load_and_resize_image(self, file_path: str, target_size: QSize) -> QImage:
        """Load and resize an image to the target size."""
        try:
            reader = QImageReader(file_path)
            reader.setAutoTransform(True)

            target_width = target_size.width()
            target_height = target_size.height()

            if target_width > 0 and target_height > 0:
                original_size = reader.size()
                if original_size.isValid():
                    scale_x = target_width / original_size.width()
                    scale_y = target_height / original_size.height()
                    scale = min(scale_x, scale_y)

                    if scale < 1.0:
                        new_width = max(1, int(original_size.width() * scale))
                        new_height = max(1, int(original_size.height() * scale))
                        reader.setScaledSize(QSize(new_width, new_height))

            image = reader.read()
            if image.isNull():
                return QImage()

            if image.format() not in (
                QImage.Format_RGB32,
                QImage.Format_ARGB32,
                QImage.Format_ARGB32_Premultiplied,
                QImage.Format_RGBA8888,
                QImage.Format_RGBA8888_Premultiplied,
            ):
                image = image.convertToFormat(QImage.Format_RGBA8888)

            return image

        except Exception as exc:  # pragma: no cover - fallback for unexpected reader failures
            print(f"Error processing image {file_path}: {exc}")
            return QImage()


class ListViewImageLoader:
    """Lazy image loader for QListWidget that loads thumbnails only when visible."""

    def __init__(self, list_widget: QListWidget, avatar_size: tuple = (60, 80)):
        self.list_widget = list_widget
        self.avatar_size = QSize(*avatar_size)

        # Track loaded images and visible items
        self.loaded_images: dict[int, QImage] = {}
        self._load_order: deque[int] = deque()
        self.visible_items: Set[int] = set()
        self.file_paths: list[str] = []
        self.cards: list = []  # Reference to the actual card widgets

        # Worker thread for background loading
        self.worker_thread = QThread()
        self.worker = ImageLoadWorker()
        self.worker.moveToThread(self.worker_thread)
        self.worker.image_loaded.connect(self._on_image_loaded)
        self.worker_thread.start()  # Start thread immediately

        # Timer for debouncing scroll events
        self.update_timer = QTimer()
        self.update_timer.setSingleShot(True)
        self.update_timer.setInterval(40)
        self.update_timer.timeout.connect(self._update_visible_items)

        # Connect to list widget scroll events
        if hasattr(self.list_widget, "verticalScrollBar"):
            scrollbar = self.list_widget.verticalScrollBar()
            if scrollbar:
                scrollbar.valueChanged.connect(self._on_scroll)

        # Configuration
        self.max_loaded_images = 150  # Maximum images to keep in memory
        self.preload_buffer = 3  # Number of items to preload outside visible area

    def set_file_paths(self, file_paths: list[str], cards: list):
        """Set the file paths and card references for lazy loading."""
        cards_copy = cards.copy() if cards else []

        self.clear()
        self.file_paths = file_paths.copy()
        self.cards = cards_copy

        if not self.worker_thread.isRunning():
            self.worker_thread.start()

        self._schedule_update()

    def clear(self):
        """Clear all loaded images and reset state."""
        self.loaded_images.clear()
        self._load_order.clear()
        self.visible_items.clear()
        self.file_paths.clear()
        self.cards.clear()

        if self.worker:
            self.worker.clear_queue()

    def _on_scroll(self):
        """Handle scroll events with debouncing."""
        self._schedule_update()

    def _schedule_update(self):
        """Schedule an update of visible items."""
        self.update_timer.start()

    def _update_visible_items(self):
        """Update which items are visible and manage loading/unloading."""
        if not self.list_widget or not self.file_paths:
            return

        new_visible_items = self._get_visible_item_indices()

        items_to_load: Set[int] = set()
        for index in new_visible_items:
            start_idx = max(0, index - self.preload_buffer)
            end_idx = min(len(self.file_paths), index + self.preload_buffer + 1)
            items_to_load.update(range(start_idx, end_idx))

        for index in items_to_load:
            if 0 <= index < len(self.file_paths):
                self._queue_image_load(index)

        self._manage_memory(items_to_load)

        self.visible_items = new_visible_items

    def _get_visible_item_indices(self) -> Set[int]:
        """Get indices of currently visible items."""
        visible_indices: Set[int] = set()

        if not self.list_widget:
            return visible_indices

        viewport_rect = self.list_widget.viewport().rect()

        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            if not item:
                continue
            item_rect = self.list_widget.visualItemRect(item)
            if viewport_rect.intersects(item_rect):
                visible_indices.add(i)

        return visible_indices

    def _queue_image_load(self, index: int, force: bool = False):
        """Queue an image for loading."""
        if not (0 <= index < len(self.file_paths)):
            return

        if not force and index in self.loaded_images:
            return

        file_path = self.file_paths[index]
        if not os.path.exists(file_path):
            return

        self.worker.add_to_queue(index, file_path, self.avatar_size, priority=force)

        if not self.worker_thread.isRunning():
            self.worker_thread.start()

    def _on_image_loaded(self, index: int, image: QImage):
        """Handle when an image has been loaded."""
        if not (0 <= index < len(self.cards)):
            return

        self.loaded_images[index] = image

        if index in self._load_order:
            self._load_order.remove(index)
        self._load_order.append(index)

        self._apply_image_to_card(index, image)

    def _apply_image_to_card(self, index: int, image: QImage):
        if not (0 <= index < len(self.cards)):
            return

        card = self.cards[index]
        if not card or not hasattr(card, "_avatar"):
            return

        if hasattr(card, "set_avatar_size"):
            card.set_avatar_size(self.avatar_size)
        pixmap = self._scaled_pixmap(image)
        if hasattr(card, "set_thumbnail_pixmap"):
            card.set_thumbnail_pixmap(pixmap)
        else:
            card._avatar.set_dayu_image(pixmap)
        card._avatar.setVisible(True)

        list_item = self.list_widget.item(index)
        if list_item:
            size_hint = card.sizeHint()
            card.setFixedHeight(size_hint.height())
            list_item.setSizeHint(size_hint)
            self.list_widget.updateGeometries()

    def _scaled_pixmap(self, image: QImage) -> QPixmap:
        if image.isNull():
            return QPixmap()
        scaled = image.scaled(self.avatar_size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        return QPixmap.fromImage(scaled)

    def _manage_memory(self, needed_items: Set[int]):
        """Manage memory by unloading images that are no longer needed."""
        if len(self.loaded_images) <= self.max_loaded_images:
            return

        attempts = len(self._load_order)
        for _ in range(attempts):
            if len(self.loaded_images) <= self.max_loaded_images:
                break
            if not self._load_order:
                break

            index = self._load_order.popleft()
            if index in needed_items:
                self._load_order.append(index)
                continue

            self.loaded_images.pop(index, None)

            if 0 <= index < len(self.cards):
                card = self.cards[index]
                if card and hasattr(card, "_avatar"):
                    card._avatar.setVisible(False)

    def update_avatar_size(self, avatar_size):
        """Update the target avatar size and refresh loaded thumbnails."""
        if isinstance(avatar_size, tuple):
            avatar_size = QSize(*avatar_size)

        if not isinstance(avatar_size, QSize) or not avatar_size.isValid():
            return

        if avatar_size == self.avatar_size:
            return

        previous_size = QSize(self.avatar_size)
        increasing = (
            avatar_size.width() > previous_size.width()
            or avatar_size.height() > previous_size.height()
        )

        self.avatar_size = avatar_size

        for index, card in enumerate(self.cards):
            if hasattr(card, "set_avatar_size"):
                card.set_avatar_size(self.avatar_size)
                size_hint = card.sizeHint()
                card.setFixedHeight(size_hint.height())
                if 0 <= index < self.list_widget.count():
                    item = self.list_widget.item(index)
                    if item:
                        item.setSizeHint(size_hint)

        for index, image in list(self.loaded_images.items()):
            self._apply_image_to_card(index, image)

        if increasing:
            for index in self.visible_items:
                self._queue_image_load(index, force=True)

        self._schedule_update()

    def force_load_image(self, index: int):
        """Force load an image immediately (for current selection)."""
        if 0 <= index < len(self.file_paths):
            self._queue_image_load(index, force=True)

    def shutdown(self):
        """Shutdown the loader and clean up resources."""
        if self.worker:
            self.worker.stop()

        if self.worker_thread.isRunning():
            self.worker_thread.quit()
            self.worker_thread.wait(5000)  # Wait up to 5 seconds

        self.clear()
