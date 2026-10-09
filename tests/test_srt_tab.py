"""test_srt_tab: SrtTabWidget の GUI テスト。

対象: text_to_shaberi.client.ui.srt_tab（未実装・TDD）
QT_QPA_PLATFORM=offscreen。widget 初期値・メソッド検証のみ。
"""
from __future__ import annotations

from text_to_shaberi.client.srt_parser import SubtitleEntry
from text_to_shaberi.client.ui.srt_tab import SrtTabWidget


class TestSrtTabCreation:
    def test_import_creates_widget(self, qapp):
        tab = SrtTabWidget()
        assert tab is not None

    def test_initial_play_button_disabled(self, qapp):
        tab = SrtTabWidget()
        assert not tab.play_button.isEnabled()

    def test_initial_stop_button_disabled(self, qapp):
        tab = SrtTabWidget()
        assert not tab.stop_button.isEnabled()


class TestSrtTabSetEntries:
    def test_table_row_count(self, qapp):
        tab = SrtTabWidget()
        entries = [
            SubtitleEntry(index=1, start_ms=0, end_ms=1000, text="A"),
            SubtitleEntry(index=2, start_ms=1500, end_ms=2500, text="B"),
            SubtitleEntry(index=3, start_ms=3000, end_ms=4000, text="C"),
        ]
        tab.set_entries(entries)
        assert tab.table.rowCount() == 3

    def test_play_button_enabled_after_entries(self, qapp):
        tab = SrtTabWidget()
        entries = [
            SubtitleEntry(index=1, start_ms=0, end_ms=1000, text="A"),
        ]
        tab.set_entries(entries)
        assert tab.play_button.isEnabled()


class TestSrtTabPlayingState:
    def test_set_playing_true(self, qapp):
        tab = SrtTabWidget()
        entries = [SubtitleEntry(index=1, start_ms=0, end_ms=1000, text="A")]
        tab.set_entries(entries)
        tab.set_playing(True)
        assert not tab.load_file_button.isEnabled()
        assert tab.stop_button.isEnabled()

    def test_set_playing_false(self, qapp):
        tab = SrtTabWidget()
        entries = [SubtitleEntry(index=1, start_ms=0, end_ms=1000, text="A")]
        tab.set_entries(entries)
        tab.set_playing(True)
        tab.set_playing(False)
        assert tab.load_file_button.isEnabled()
        assert not tab.stop_button.isEnabled()

    def test_set_paused_true(self, qapp):
        tab = SrtTabWidget()
        entries = [SubtitleEntry(index=1, start_ms=0, end_ms=1000, text="A")]
        tab.set_entries(entries)
        tab.set_playing(True)
        tab.set_paused(True)
        assert tab.pause_button.isChecked() or tab.pause_button.text() != "一時停止"

    def test_set_paused_false(self, qapp):
        tab = SrtTabWidget()
        entries = [SubtitleEntry(index=1, start_ms=0, end_ms=1000, text="A")]
        tab.set_entries(entries)
        tab.set_playing(True)
        tab.set_paused(True)
        tab.set_paused(False)
        assert not (tab.pause_button.isChecked() or False)

    def test_progress_bar_visible(self, qapp):
        tab = SrtTabWidget()
        assert tab.progress_bar is not None

    def test_set_current_entry_updates_progress(self, qapp):
        tab = SrtTabWidget()
        entries = [
            SubtitleEntry(index=1, start_ms=0, end_ms=1000, text="A"),
            SubtitleEntry(index=2, start_ms=1500, end_ms=2500, text="B"),
            SubtitleEntry(index=3, start_ms=3000, end_ms=4000, text="C"),
        ]
        tab.set_entries(entries)
        tab.set_current_entry(1, 3)
        assert tab.progress_bar.value() == int(2 / 3 * 100)

    def test_set_current_entry_highlights_row(self, qapp):
        tab = SrtTabWidget()
        entries = [
            SubtitleEntry(index=1, start_ms=0, end_ms=1000, text="A"),
            SubtitleEntry(index=2, start_ms=1500, end_ms=2500, text="B"),
        ]
        tab.set_entries(entries)
        tab.set_current_entry(0, 2)
        assert len(tab.table.selectedItems()) > 0

    def test_pause_button_initially_disabled(self, qapp):
        tab = SrtTabWidget()
        assert not tab.pause_button.isEnabled()

    def test_wait_playback_check_initial_state(self, qapp):
        tab = SrtTabWidget()
        assert tab.wait_playback_check.isChecked()
        assert tab.wait_playback_check.isCheckable()
