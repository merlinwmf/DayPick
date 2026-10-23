"""
Desktop UI for DayPick - HTML/CSS/JS rendered via pywebview, backed by
picker_engine.py. All layout/styling lives in web/daypick.html; this file
is just the JS<->Python bridge (the `Api` class, exposed as `js_api`) plus
pushing live updates into the page via window.evaluate_js().

Run: python daypick_app.py
"""

import sys
import json
import threading
from datetime import timedelta
from pathlib import Path

import webview
import picker_engine as engine

if getattr(sys, 'frozen', False):
    # Running as a PyInstaller exe: __file__/sys.executable resolve to the
    # temp extraction / exe folder, not where web/daypick.html ships from.
    APP_DIR = Path(sys.executable).resolve().parent
    WEB_DIR = Path(getattr(sys, '_MEIPASS', APP_DIR)) / 'web'
else:
    APP_DIR = Path(__file__).resolve().parent
    WEB_DIR = APP_DIR / 'web'

CONFIG_PATH = APP_DIR / 'daypick_config.json'


class Api:
    """Exposed to the page as `pywebview.api`. Methods called from JS run on
    pywebview's own worker thread, so long engine calls still spawn their
    own background thread to avoid blocking further JS->Python calls."""

    def __init__(self):
        self.window = None
        self.ctx = None
        self.logics = engine.default_pick_logics()
        self.selected_groups = set()
        self.all_groups = []
        self.log_range = 'today'
        self.running = False
        self.market_cap_enabled = True
        self.market_cap_threshold = engine.MARKET_CAP_DEFAULT
        self._load_config()

    # --------------------------------------------------------- config
    def _load_config(self):
        if not CONFIG_PATH.exists():
            return
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            return
        self.selected_groups = set(data.get('selected_groups', []))
        self.market_cap_enabled = data.get('market_cap_enabled', True)
        self.market_cap_threshold = data.get('market_cap_threshold', engine.MARKET_CAP_DEFAULT)
        saved_logics = data.get('logics', {})
        for logic in self.logics:
            saved = saved_logics.get(logic.id)
            if saved:
                logic.name = saved.get('name', logic.name)
                logic.enabled = saved.get('enabled', logic.enabled)

    def _save_config(self):
        data = {
            'selected_groups': sorted(self.selected_groups),
            'market_cap_enabled': self.market_cap_enabled,
            'market_cap_threshold': self.market_cap_threshold,
            'logics': {l.id: {'name': l.name, 'enabled': l.enabled} for l in self.logics},
        }
        try:
            CONFIG_PATH.write_text(json.dumps(data, indent=2), encoding='utf-8')
        except OSError:
            pass

    # ---------------------------------------------------- push to JS
    def _push(self, fn, *args):
        if self.window is None:
            return
        payload = ', '.join(json.dumps(a) for a in args)
        try:
            self.window.evaluate_js(f'{fn}({payload})')
        except Exception:
            pass

    def _status(self, text):
        self._push('appStatus', text)

    # ------------------------------------------------- exposed to JS
    def get_initial_state(self):
        return {
            'logics': [{'id': l.id, 'name': l.name, 'description': l.description, 'enabled': l.enabled,
                        'market_cap_override': l.market_cap_override}
                       for l in self.logics],
            'selected_groups': sorted(self.selected_groups),
            'log_range': self.log_range,
            'market_cap_enabled': self.market_cap_enabled,
            'market_cap_threshold': self.market_cap_threshold,
            'market_cap_presets': engine.MARKET_CAP_PRESETS,
        }

    def start_app(self):
        """Kicks off the OpenD connect + watchlist load. Called once the
        page has finished booting (see the pywebviewready listener in
        daypick.html)."""
        def worker():
            try:
                self.ctx = engine.connect(timeout_seconds=60, poll_cb=self._status)
                self._status('Connected. Loading custom watchlists...')
                groups = engine.get_custom_groups(self.ctx)
                self.all_groups = groups
                self._push('appGroups', groups)
                if not groups:
                    self._status('Connected, but no custom watchlists found in Moomoo.')
                    return
                self._status('Ready.')
                self._recompute_pool_count()
                self._refresh_logs()
            except engine.EngineError as e:
                self._status(f'Error: {e}')
        threading.Thread(target=worker, daemon=True).start()

    def toggle_pool(self, group_name):
        if group_name in self.selected_groups:
            self.selected_groups.discard(group_name)
        else:
            self.selected_groups.add(group_name)
        self._save_config()
        self._recompute_pool_count()

    def _recompute_pool_count(self):
        if not self.ctx or not self.selected_groups:
            self._push('appPoolCount', len(self.selected_groups), 0)
            return

        def worker():
            try:
                pool = engine.get_pooled_members(self.ctx, list(self.selected_groups))
                self._push('appPoolCount', len(self.selected_groups), len(pool))
            except engine.EngineError as e:
                self._status(f'Error: {e}')
        threading.Thread(target=worker, daemon=True).start()

    def toggle_logic(self, logic_id):
        for logic in self.logics:
            if logic.id == logic_id:
                logic.enabled = not logic.enabled
        self._save_config()

    def rename_logic(self, logic_id, new_name):
        new_name = (new_name or '').strip()
        if not new_name:
            return
        for logic in self.logics:
            if logic.id == logic_id:
                logic.name = new_name
        self._save_config()

    # ------------------------------------------------- market cap filter
    def set_market_cap_enabled(self, enabled):
        self.market_cap_enabled = bool(enabled)
        self._save_config()

    def set_market_cap_threshold(self, threshold):
        self.market_cap_threshold = float(threshold)
        self._save_config()

    def start_pick(self):
        if self.running:
            return
        if not self.ctx or not self.selected_groups:
            self._status('Not connected, or no stock pool selected.')
            return
        self.running = True
        self._push('appRunning', True)

        def worker():
            try:
                engine.run_pick(
                    self.ctx, list(self.selected_groups), self.logics,
                    market_cap_enabled=self.market_cap_enabled,
                    market_cap_threshold=self.market_cap_threshold,
                    status_cb=self._status,
                    progress_cb=lambda i, n, code: self._status(
                        f'Evaluating {i}/{n}: {engine.to_symbol(code)}...'),
                )
            except engine.EngineError as e:
                self._status(f'Error: {e}')
            self.running = False
            self._push('appRunning', False)
            self._refresh_logs()

        threading.Thread(target=worker, daemon=True).start()

    def set_log_range(self, range_key):
        self.log_range = range_key
        self._refresh_logs()

    def _refresh_logs(self):
        today = engine.now_et().date()
        if self.log_range == 'today':
            dates = [today]
        elif self.log_range == 'yesterday':
            dates = [today - timedelta(days=1)]
        else:
            dates = [today - timedelta(days=i) for i in range(2, -1, -1)]

        events = []
        for d in dates:
            events.extend(engine.read_pick_events(str(APP_DIR), d))
        events.sort(key=lambda e: e['time'])
        self._push('appResults', events)


def main():
    engine.configure_logging(str(APP_DIR))
    api = Api()
    window = webview.create_window(
        'DayPick', str(WEB_DIR / 'daypick.html'),
        width=650, height=960, min_size=(560, 680), js_api=api,
    )
    api.window = window
    webview.start(debug=False)


if __name__ == '__main__':
    main()
