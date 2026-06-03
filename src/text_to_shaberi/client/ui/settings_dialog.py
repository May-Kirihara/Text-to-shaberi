"""歯車設定ダイアログ（最小版）。

サーバWSアドレス・トークン・プロファイル を設定し、OK で values() を返す。
"""
from __future__ import annotations

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLineEdit,
)


class SettingsDialog(QDialog):
    def __init__(self, parent, cfg, profiles=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("設定")

        layout = QFormLayout(self)

        self.address_edit = QLineEdit()
        self.address_edit.setText(getattr(cfg.server, "address", ""))
        layout.addRow("サーバWSアドレス:", self.address_edit)

        self.token_edit = QLineEdit()
        self.token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.token_edit.setText(getattr(cfg.server, "token", ""))
        layout.addRow("トークン:", self.token_edit)

        self.profile_combo = QComboBox()
        self.profile_combo.addItem("(サーバ既定)", "")
        active_id = getattr(cfg.profile, "active_id", "")
        selected_idx = 0
        for pi in profiles or []:
            self.profile_combo.addItem(pi.name if pi.name else pi.id, pi.id)
            if pi.id == active_id:
                selected_idx = self.profile_combo.count() - 1
        self.profile_combo.setCurrentIndex(selected_idx)
        layout.addRow("プロファイル:", self.profile_combo)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def values(self) -> dict:
        return {
            "address": self.address_edit.text(),
            "token": self.token_edit.text(),
            "active_id": self.profile_combo.currentData() or "",
        }
