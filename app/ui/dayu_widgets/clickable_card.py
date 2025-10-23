# Import future modules
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

# Import third-party modules
from PySide6 import QtCore
from PySide6 import QtGui
from PySide6 import QtWidgets

# Import local modules
from . import dayu_theme
from .avatar import MAvatar
from .divider import MDivider
from .label import MLabel
from .mixin import cursor_mixin
from .mixin import hover_shadow_mixin
from .tool_button import MToolButton


@hover_shadow_mixin
@cursor_mixin
class ClickCard(QtWidgets.QWidget):
    clicked = QtCore.Signal()
    extra_button_clicked = QtCore.Signal()

    def __init__(self, title=None, image=None, size=None, extra=None, type=None, parent=None):
        super(ClickCard, self).__init__(parent=parent)
        self.setAttribute(QtCore.Qt.WA_StyledBackground)
        self.setProperty("border", False)
        size = size or dayu_theme.default_size
        map_label = {
            dayu_theme.large: (MLabel.H2Level, 20),
            dayu_theme.medium: (MLabel.H3Level, 15),
            dayu_theme.small: (MLabel.H4Level, 10),
        }
        self._title_label = MLabel(text=title)
        self._title_label.set_dayu_level(map_label.get(size)[0])
        self._title_label.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents)  # Make title label transparent to mouse events

        padding = map_label.get(size)[-1]
        self._title_layout = QtWidgets.QHBoxLayout()
        self._title_layout.setContentsMargins(padding, padding, padding, padding)
        if image:
            self._title_icon = MAvatar()
            self._title_icon.set_dayu_image(image)
            self._title_icon.set_dayu_size(size)
            self._title_icon.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents)  # Make avatar transparent to mouse events
            self._title_layout.addWidget(self._title_icon)
        self._title_layout.addWidget(self._title_label)
        self._title_layout.addStretch()
        
        self._extra_button = None
        if extra:
            self._extra_button = MToolButton().icon_only().svg("more.svg")
            self._extra_button.clicked.connect(self._on_extra_button_clicked) # Handle extra button click
            self._title_layout.addWidget(self._extra_button)

        self._content_layout = QtWidgets.QVBoxLayout()

        self._main_lay = QtWidgets.QVBoxLayout()
        self._main_lay.setSpacing(0)
        self._main_lay.setContentsMargins(1, 1, 1, 1)
        if title:
            self._main_lay.addLayout(self._title_layout)
            self._main_lay.addWidget(MDivider())
        self._main_lay.addLayout(self._content_layout)
        self.setLayout(self._main_lay)

    def get_more_button(self):
        return self._extra_button

    def _on_extra_button_clicked(self):
        self.extra_button_clicked.emit()

    def set_widget(self, widget):
        widget.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents)  # Make new widget transparent to mouse events
        self._content_layout.addWidget(widget)

    def border(self):
        self.setProperty("border", True)
        self.style().polish(self)
        return self

    def mousePressEvent(self, event):
        if event.button() == QtCore.Qt.LeftButton:
            # Check if the click is on the extra button if it exists
            if self._extra_button and self._extra_button.geometry().contains(event.pos()):
                # Let the extra button handle this event
                return
            self.clicked.emit()
        super(ClickCard, self).mousePressEvent(event)

@hover_shadow_mixin
@cursor_mixin
class ClickMeta(QtWidgets.QWidget):
    clicked = QtCore.Signal()
    extra_button_clicked = QtCore.Signal()

    def __init__(
        self,
        cover=None,
        avatar=None,
        title=None,
        description=None,
        extra=False,
        parent=None,
        avatar_size=()
    ):
        super(ClickMeta, self).__init__(parent)
        self.setAttribute(QtCore.Qt.WA_StyledBackground)

        # Initialize Widgets
        self._cover_label = QtWidgets.QLabel()
        self._avatar = MAvatar()
        self._title_label = MLabel().secondary()
        self._description_label = MLabel().secondary()
        self._description_label.setWordWrap(True)
        self._description_label.set_elide_mode(QtCore.Qt.ElideRight)

        self._title_layout = QtWidgets.QHBoxLayout()
        self._title_layout.addWidget(self._title_label)
        self._title_layout.addStretch()

        self._extra = extra
        self._extra_button = MToolButton(parent=self).icon_only().svg("more.svg") 
        self._title_layout.addWidget(self._extra_button)
        self._extra_button.setVisible(extra)
        if self._extra:
            self._extra_button.clicked.connect(self._on_extra_button_clicked)

        content_lay = QtWidgets.QVBoxLayout()
        content_lay.addLayout(self._title_layout)
        content_lay.addWidget(self._description_label)

        avatar_layout = QtWidgets.QVBoxLayout()
        avatar_layout.setContentsMargins(0, 0, 0, 0)
        avatar_layout.setSpacing(0)
        avatar_layout.addWidget(self._avatar, alignment=QtCore.Qt.AlignTop)

        avatar_content_layout = QtWidgets.QHBoxLayout()
        avatar_content_layout.addSpacing(2)
        avatar_content_layout.addLayout(avatar_layout)
        avatar_content_layout.addSpacing(3)
        avatar_content_layout.addLayout(content_lay)

        self._button_layout = QtWidgets.QHBoxLayout()

        main_lay = QtWidgets.QVBoxLayout()
        main_lay.setSpacing(0)
        main_lay.setContentsMargins(1, 1, 1, 1)
        main_lay.addWidget(self._cover_label)
        main_lay.addLayout(avatar_content_layout)
        main_lay.addLayout(self._button_layout)
        self.setLayout(main_lay)
        self._cover_label.setFixedSize(QtCore.QSize(200, 200))

        # Remember the preferred avatar size so the layout can react to real thumbnail
        # dimensions once they are available.
        self._avatar_preferred_size = QtCore.QSize()
        if avatar_size:
            self.set_avatar_size(avatar_size)
        # Make widgets transparent for mouse events (excluding extra button)
        self._make_widgets_transparent()

    def _make_widgets_transparent(self):
        for widget in self.findChildren(QtWidgets.QWidget):
            if widget != self._extra_button:
                widget.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents)

    def _on_extra_button_clicked(self):
        self.extra_button_clicked.emit()

    def setup_data(self, data_dict):
        has_title = data_dict.get("title") is not None
        has_description = data_dict.get("description") is not None

        if has_title:
            self._title_label.setText(data_dict.get("title"))
            self._title_label.setVisible(True)
        else:
            self._title_label.setVisible(False)

        if has_description:
            self._description_label.setText(data_dict.get("description"))
            self._description_label.setVisible(True)
        else:
            self._description_label.setVisible(False)
        
        if data_dict.get("avatar"):
            self._avatar.set_dayu_image(data_dict.get("avatar"))
            self._avatar.setVisible(True)
        else:
            self._avatar.setVisible(False)

        if data_dict.get("cover"):
            fixed_height = self._cover_label.width()
            self._cover_label.setPixmap(
                data_dict.get("cover").scaledToWidth(fixed_height, QtCore.Qt.SmoothTransformation)
            )
            self._cover_label.setVisible(True)
        else:
            self._cover_label.setVisible(False)

        if "clicked" in data_dict and callable(data_dict["clicked"]):
            self.connect_clicked(data_dict["clicked"])

        if "extra_clicked" in data_dict and callable(data_dict["extra_clicked"]):
            self.connect_extra_clicked(data_dict["extra_clicked"])

    def connect_clicked(self, func):
        self.clicked.connect(func)

    def connect_extra_clicked(self, func):
        if self._extra:
            self._extra_button.clicked.connect(func)
            self._extra_button.setVisible(True)

    def mousePressEvent(self, event):
        if event.button() == QtCore.Qt.LeftButton:
            # Check if the click is on the extra button if it exists
            if self._extra_button.isVisible() and self._extra_button.geometry().contains(event.pos()):
                # Let the extra button handle this event
                return
            self.clicked.emit()
        super(ClickMeta, self).mousePressEvent(event)

    def set_highlight(self, highlighted):
        if not hasattr(self, '_original_background_color'):
            self._original_background_color = self.palette().color(self.backgroundRole())
        
        if highlighted:
            highlight_color = self._original_background_color.darker(130)
            self.setStyleSheet(f"background-color: {highlight_color.name()};")
        else:
            self.setStyleSheet(f"background-color: {self._original_background_color.name()};")

        self.update()

    def set_skipped(self, skipped: bool):
        """Visually mark / un-mark this row as skipped."""
        font = self._title_label.font()
        font.setStrikeOut(skipped)
        self._title_label.setFont(font)

        # grey text if skipped, default otherwise
        if skipped:
            self._title_label.setStyleSheet("color: gray;")
        else:
            self._title_label.setStyleSheet("")

    def sizeHint(self):
        """Return appropriate size hint based on avatar size and content."""
        # If a cover is visible it sits above content and controls the width/height mainly
        cover_size = self._cover_label.size() if self._cover_label.isVisible() else QtCore.QSize(0, 0)

        # Avatar dimensions: use the actual widget size when visible
        if self._avatar.isVisible():
            avatar_size = self._avatar.size()
            avatar_width, avatar_height = avatar_size.width(), avatar_size.height()
        else:
            avatar_width = 0
            avatar_height = 0

        # Content dimensions
        title_height = self._title_label.sizeHint().height() if self._title_label.isVisible() else 0
        desc_height = self._description_label.sizeHint().height() if self._description_label.isVisible() else 0
        content_height = title_height + desc_height + 10  # spacing/margins

        # Total height: if cover visible, stack cover above content; otherwise rely on avatar/content
        if not cover_size.isEmpty():
            total_height = cover_size.height() + content_height + 10
        else:
            # If there's an avatar, keep a small extra padding; if not, don't add extra padding
            if avatar_height:
                total_height = max(avatar_height, content_height)
            else:
                total_height = content_height

        # Total width: max of cover width (if visible) or avatar+content width
        content_width = max(
            self._title_label.sizeHint().width() if self._title_label.isVisible() else 0,
            self._description_label.sizeHint().width() if self._description_label.isVisible() else 0,
        )
        # If there's an avatar include spacing between avatar and content; if not, don't add extra padding
        if avatar_width:
            stacked_width = avatar_width + content_width + 20
        else:
            stacked_width = content_width
        total_width = max(cover_size.width(), stacked_width)

        # Respect the preferred width so list widgets can enforce full-width thumbnails.
        if self._avatar_preferred_size.width():
            total_width = max(total_width, self._avatar_preferred_size.width())

        return QtCore.QSize(max(total_width, 150), total_height)

    def set_avatar_size(self, avatar_size):
        """Update the avatar size tracking so size hints remain in sync."""
        if isinstance(avatar_size, QtCore.QSize):
            width, height = avatar_size.width(), avatar_size.height()
        elif isinstance(avatar_size, (tuple, list)) and len(avatar_size) == 2:
            width, height = avatar_size
        else:
            width = height = 0

        if width:
            width = int(width)
            height = int(height) if height else width
            placeholder = QtCore.QSize(width, max(1, height))
            self._avatar_preferred_size = placeholder
            self._avatar.setFixedSize(placeholder)
        else:
            self._avatar_preferred_size = QtCore.QSize()
        self.updateGeometry()

    def set_thumbnail_pixmap(self, pixmap: QtGui.QPixmap):
        """Assign the thumbnail pixmap and resize the avatar to the image height."""
        if pixmap and not pixmap.isNull():
            image_size = pixmap.size()
            self._avatar.setFixedSize(image_size)
            self._avatar_preferred_size = QtCore.QSize(image_size.width(), image_size.height())
            self._avatar.set_dayu_image(pixmap)
        else:
            # Revert to the preferred size when there's no valid thumbnail yet.
            if self._avatar_preferred_size.isValid():
                self._avatar.setFixedSize(self._avatar_preferred_size)
            self._avatar.set_dayu_image(pixmap)
        self._avatar.updateGeometry()
        self.updateGeometry()
