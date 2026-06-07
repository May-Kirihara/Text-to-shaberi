"""SRT タブウィジェット。"""
from __future__ import annotations

from PySide6.QtWidgets import (
    QHBoxLayout,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)


class SrtTabWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)

        layout = QVBoxLayout(self)

        btn_row = QHBoxLayout()
        self.load_file_button = QPushButton("ファイル選択")
        self.play_button = QPushButton("再生")
        self.play_button.setEnabled(False)
        self.pause_button = QPushButton("一時停止")
        self.pause_button.setCheckable(True)
        self.pause_button.setEnabled(False)
        self.stop_button = QPushButton("停止")
        self.stop_button.setEnabled(False)
        btn_row.addWidget(self.load_file_button)
        btn_row.addWidget(self.play_button)
        btn_row.addWidget(self.pause_button)
        btn_row.addWidget(self.stop_button)
        layout.addLayout(btn_row)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["#", "時間", "テキスト"])
        layout.addWidget(self.table)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        layout.addWidget(self.progress_bar)

    def set_entries(self, entries) -> None:
        self.table.setRowCount(len(entries))
        for row, entry in enumerate(entries):
            self.table.setItem(row, 0, QTableWidgetItem(str(entry.index)))
            start_s = entry.start_ms / 1000.0
            end_s = entry.end_ms / 1000.0
            self.table.setItem(row, 1, QTableWidgetItem(f"{start_s:.1f} - {end_s:.1f}"))
            self.table.setItem(row, 2, QTableWidgetItem(entry.text))
        self.play_button.setEnabled(True)

    def set_playing(self, playing: bool) -> None:
        self.load_file_button.setEnabled(not playing)
        self.play_button.setEnabled(not playing)
        self.stop_button.setEnabled(playing)
        self.pause_button.setEnabled(playing)

    def set_paused(self, paused: bool) -> None:
        self.pause_button.setChecked(paused)
        self.pause_button.setText("再開" if paused else "一時停止")

    def set_current_entry(self, entry_index: int, total: int) -> None:
        """再生中のエントリ位置を反映（プログレスバー＋テーブルハイライト）。"""
        if total > 0:
            pct = int((entry_index + 1) / total * 100)
            self.progress_bar.setValue(pct)
        self.table.clearSelection()
        if 0 <= entry_index < self.table.rowCount():
            self.table.selectRow(entry_index)
            self.table.scrollToItem(self.table.item(entry_index, 0))
