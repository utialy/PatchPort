"""Small Tk management application backed exclusively by the core JSON API."""
import argparse
import os
from pathlib import Path
import sys
import time
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

from desktop.client import CoreClient, Worker, bundled_core
from desktop.model import Projects, response_text, status_text, task_values


class Application:
    def __init__(self, root, worker, projects):
        self.root, self.worker, self.projects = root, worker, projects
        self.plan = None
        self.discard_plan = None
        self.plan_valid = False
        self.plan_deadline = 0
        self.status = None
        self.task_config = None
        self.pending_args = None
        self.started = 0
        self.controls = []
        root.title('PatchPort')
        root.geometry('1000x900')
        root.minsize(800, 800)
        frame = ttk.Frame(root, padding=16)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text='PatchPort', font=('Segoe UI', 22, 'bold')).pack(anchor='w')
        ttk.Label(frame, text='Connect your project. Manage local collaboration.').pack(anchor='w')
        row = ttk.Frame(frame)
        row.pack(fill='x', pady=10)
        self.project = tk.StringVar()
        self.selector = ttk.Combobox(row, textvariable=self.project, values=projects.items, state='readonly')
        self.selector.pack(side='left', fill='x', expand=True)
        self.selector.bind('<<ComboboxSelected>>', lambda event: self.changed_project())
        self.button(row, 'Open project\u2026', self.open_project)
        self.button(row, 'New project\u2026', self.new_project)
        self.tabs = ttk.Notebook(frame)
        self.tabs.pack(fill='both', expand=True)
        setup = ttk.Frame(self.tabs, padding=12)
        manage = ttk.Frame(self.tabs, padding=12)
        self.manage_tab = manage
        self.tabs.add(setup, text='Connection')
        self.tabs.add(manage, text='Status & results')
        ttk.Label(setup, text='Existing bridge.json settings are preserved. For a new connection, select CLIs and input files.',
                  wraplength=850).pack(anchor='w')
        self.providers = {}
        self.launches = {}
        self.shell_selectors = []
        ttk.Label(setup, text='Enter a command name or path. Select a profile shell for functions/aliases such as claude-1.',
                  wraplength=850).pack(anchor='w')
        for provider in ('codex', 'claude'):
            row = ttk.Frame(setup)
            row.pack(fill='x', pady=4)
            ttk.Label(row, text=provider.title(), width=10).pack(side='left')
            variable = tk.StringVar()
            entry = ttk.Entry(row, textvariable=variable)
            entry.pack(side='left', fill='x', expand=True)
            self.controls.append(entry)
            self.providers[provider] = variable
            variable.trace_add('write', self.invalidate_plan)
            self.button(row, 'Find', lambda p=provider: self.send('discover', {'provider': p}))
            self.button(row, 'Choose\u2026', lambda p=provider: self.choose_cli(p))
            shell = tk.StringVar(value='direct')
            selector = ttk.Combobox(row, textvariable=shell, values=('direct', 'powershell', 'pwsh', 'bash', 'zsh'),
                                    state='readonly', width=12)
            selector.pack(side='left', padx=4)
            self.controls.append(selector)
            self.shell_selectors.append(selector)
            shell.trace_add('write', self.invalidate_plan)
            options = {'shell': shell}
            row = ttk.Frame(setup)
            row.pack(fill='x')
            for key, label in (('executable', 'Shell path (optional)'), ('profile', 'Profile path (optional)')):
                ttk.Label(row, text=label).pack(side='left')
                value = tk.StringVar()
                field = ttk.Entry(row, textvariable=value)
                field.pack(side='left', fill='x', expand=True, padx=4)
                value.trace_add('write', self.invalidate_plan)
                self.controls.append(field)
                options[key] = value
            self.launches[provider] = options
        self.include = tk.StringVar()
        self.writable = tk.StringVar()
        for label, variable in [('Input files (one relative path per line)', self.include),
                                ('Writable in original (one per line; blank uses inputs)', self.writable)]:
            row = ttk.Frame(setup)
            row.pack(fill='x', pady=3)
            ttk.Label(row, text=label).pack(anchor='w')
            widget = tk.Text(row, height=2, wrap='none', font=('Consolas', 10))
            widget.pack(side='left', fill='x', expand=True)
            self.controls.append(widget)
            def edited(event, text=widget, value=variable):
                if text.edit_modified():
                    value.set(text.get('1.0', 'end-1c'))
                    text.edit_modified(False)
                    self.invalidate_plan()
            widget.bind('<<Modified>>', edited)
            if variable is self.include:
                self.input_widget = widget
                self.button(row, 'Select files\u2026', self.choose_inputs)
            variable.trace_add('write', self.invalidate_plan)
        self.starter = tk.BooleanVar(value=False)
        check = ttk.Checkbutton(setup, text='Create a starter input file (new project)', variable=self.starter,
                                command=self.invalidate_plan)
        check.pack(anchor='w', pady=3)
        self.controls.append(check)
        row = ttk.Frame(setup)
        row.pack(fill='x', pady=6)
        self.button(row, 'Preview connection', self.preview)
        self.apply_button = self.button(row, 'Apply preview', self.apply)
        self.cancel_button = self.button(row, 'Discard preview', self.cancel)
        self.button(row, 'Copy first request', self.copy_request)
        self.connection = tk.StringVar(value='Choose a project to begin. No provider calls are made by setup.')
        ttk.Label(setup, textvariable=self.connection, wraplength=850).pack(anchor='w', pady=5)
        self.setup_output = self.output(setup)
        row = ttk.Frame(manage)
        row.pack(fill='x')
        self.button(row, 'Refresh status', self.refresh)
        self.button(row, 'Start runner\u2026', self.start)
        self.button(row, 'Stop after work\u2026', lambda: self.stop(False))
        self.button(row, 'Cancel work & stop\u2026', lambda: self.stop(True))
        row = ttk.Frame(manage)
        row.pack(fill='x', pady=5)
        self.button(row, 'Pause new work', lambda: self.change('pause'))
        self.button(row, 'Resume new work\u2026', lambda: self.change('resume'))
        self.button(row, 'Set call limit\u2026', self.limit)
        self.button(row, 'Read by ID\u2026', self.result)
        self.status_label = tk.StringVar(value='Refresh to inspect the selected project. Authentication is not checked.')
        ttk.Label(manage, textvariable=self.status_label, wraplength=850, justify='left').pack(anchor='w', pady=8)
        row = ttk.Frame(manage)
        row.pack(fill='x')
        self.task_note = tk.StringVar(value='Refresh tasks to find a saved result. No AI call is made.')
        ttk.Label(row, textvariable=self.task_note, wraplength=580).pack(side='left', fill='x', expand=True)
        self.button(row, 'Refresh tasks', self.refresh_tasks)
        self.read_task_button = self.button(row, 'Read selected result', self.read_selected_result)
        row = ttk.Frame(manage)
        row.pack(fill='x', pady=6)
        self.task_tree = ttk.Treeview(row, columns=('created', 'endpoint', 'state', 'id'),
                                     show='headings', height=5, selectmode='browse')
        for name, title, width in (('created', 'Created', 155), ('endpoint', 'AI', 90),
                                    ('state', 'State', 105), ('id', 'Task', 320)):
            self.task_tree.heading(name, text=title)
            self.task_tree.column(name, width=width, minwidth=60, stretch=name == 'id')
        self.task_tree.pack(side='left', fill='x', expand=True)
        scrollbar = ttk.Scrollbar(row, command=self.task_tree.yview)
        scrollbar.pack(side='right', fill='y')
        self.task_tree.configure(yscrollcommand=scrollbar.set)
        self.task_tree.bind('<<TreeviewSelect>>', lambda event: self.update_controls())
        self.task_tree.bind('<Double-1>', lambda event: self.guard(self.read_selected_result))
        self.task_tree.bind('<Return>', lambda event: self.guard(self.read_selected_result))
        self.status_output = self.output(manage)
        footer = ttk.Frame(frame)
        footer.pack(fill='x', pady=(8, 0))
        self.notice = tk.StringVar(value='Closing this window leaves runners running.')
        ttk.Label(footer, textvariable=self.notice, wraplength=720).pack(side='left', fill='x', expand=True)
        self.button(footer, 'Reconnect core', self.reconnect)
        root.protocol('WM_DELETE_WINDOW', self.close)
        self.update_controls()
        root.after(100, self.poll)

    def button(self, parent, text, command):
        widget = ttk.Button(parent, text=text, command=lambda: self.guard(command))
        widget.pack(side='left', padx=(0, 5))
        self.controls.append(widget)
        return widget

    def output(self, parent):
        frame = ttk.Frame(parent)
        frame.pack(fill='both', expand=True)
        text = tk.Text(frame, wrap='word', height=8, state='disabled', font=('Consolas', 10))
        scrollbar = ttk.Scrollbar(frame, command=text.yview)
        text.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side='right', fill='y')
        text.pack(fill='both', expand=True)
        return text

    def display(self, widget, value):
        widget.configure(state='normal')
        widget.delete('1.0', 'end')
        widget.insert('1.0', response_text(value))
        widget.configure(state='disabled')

    def guard(self, command):
        try:
            command()
        except (OSError, ValueError, RuntimeError) as exc:
            messagebox.showerror('PatchPort', str(exc), parent=self.root)

    def invalidate_plan(self, *args):
        if self.plan is not None:
            self.discard_plan = self.plan
            self.plan = None
            self.connection.set('Preview invalidated. Review a new preview before applying.')
        if hasattr(self, 'apply_button'):
            self.update_controls()

    def changed_project(self):
        self.invalidate_plan()
        self.status = None
        self.status_label.set('Project changed. Refresh status before runner operations.')
        self.connection.set('Project selected. Preview to check connection files and CLI availability.')
        self.display(self.setup_output, {})
        self.display(self.status_output, {})
        self.clear_tasks('Project changed. Refresh tasks to see its results.')

    def select_project(self, path):
        self.projects.add(path)
        self.selector.configure(values=self.projects.items)
        self.project.set(str(Path(path).resolve()))
        self.changed_project()

    def open_project(self):
        path = filedialog.askdirectory(parent=self.root, title='Select project')
        if path:
            self.select_project(path)

    def new_project(self):
        parent = filedialog.askdirectory(parent=self.root, title='Select parent folder')
        if not parent:
            return
        name = simpledialog.askstring('New project', 'Folder name (created only on Apply):', parent=self.root)
        if name:
            if name in ('.', '..') or any(c in name for c in '/\\:'):
                raise ValueError('Enter a single folder name')
            path = Path(parent) / name
            if path.exists():
                raise ValueError('Folder already exists; use Open project')
            self.select_project(path)
            self.starter.set(True)

    def selected(self):
        if not self.project.get():
            raise ValueError('Choose a project first')
        return Path(self.project.get())

    def choose_cli(self, provider):
        path = filedialog.askopenfilename(parent=self.root, title='Choose official ' + provider + ' CLI')
        if path:
            self.providers[provider].set(path)

    def choose_inputs(self):
        project = self.selected()
        paths = filedialog.askopenfilenames(parent=self.root, initialdir=project, title='Choose input files')
        if paths:
            relative = [Path(p).resolve().relative_to(project.resolve()).as_posix() for p in paths]
            self.input_widget.delete('1.0', 'end')
            self.input_widget.insert('1.0', '\n'.join(relative))
            self.include.set('\n'.join(relative))

    def preview(self):
        project = self.selected()
        args = dict(project=str(project), create_project=not project.exists())
        if not (project / 'bridge.json').exists():
            args['providers'] = {p: v.get().strip() for p, v in self.providers.items() if v.get().strip()}
            for provider, command in list(args['providers'].items()):
                values = {k: v.get().strip() for k, v in self.launches[provider].items()}
                if values['shell'] != 'direct':
                    args['providers'][provider] = {'command': command,
                                                  'launch': {k: v for k, v in values.items() if v}}
                elif values['executable'] or values['profile']:
                    raise ValueError('Select a profile shell to use shell/profile paths')
            for key, variable in [('include', self.include), ('writable', self.writable)]:
                paths = [p.strip() for p in variable.get().splitlines() if p.strip()]
                if paths:
                    args[key] = paths
            args['starter'] = self.starter.get()
        # Dispose an old plan before creating another; never exhaust core plan capacity.
        if self.plan or self.discard_plan:
            self.after_cancel = args
            self.cancel()
        else:
            self.send('setup.preview', args)

    def apply(self):
        if not self.plan or not self.plan_valid or time.monotonic() >= self.plan_deadline:
            self.invalidate_plan()
            raise ValueError('Preview expired. Preview again.')
        if messagebox.askyesno('Apply connection', 'Apply the displayed file changes to:\n' + self.project.get(), parent=self.root):
            plan, self.plan = self.plan, None
            self.send('setup.apply', dict(plan_id=plan, apply=True))

    def cancel(self):
        if self.plan or self.discard_plan:
            plan = self.plan or self.discard_plan
            self.plan = self.discard_plan = None
            self.send('setup.cancel', dict(plan_id=plan))

    def copy_request(self):
        project = self.selected()
        text = ('Use the PatchPort peer-consult integration in ' + str(project)
                + ' to ask the other AI to review the selected input files. '
                  'Show me the files and question before submitting, then bring its actual reply back here.')
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.notice.set('First request copied. Sign in using the official CLI; start the runner separately.')

    def config_args(self):
        return dict(config=str(self.selected() / 'bridge.json'))

    def refresh(self):
        self.send('runner.status', self.config_args())

    def start(self):
        if self.status is None:
            raise ValueError('Refresh status before starting')
        if messagebox.askyesno('Start runner', self.project.get() + '\n\n' + status_text(self.status)
                + '\n\nQueued work may invoke providers and consume usage. Start?', parent=self.root):
            self.send('runner.start', dict(self.config_args(), apply=True))

    def stop(self, cancel):
        record = (self.status or {}).get('management') or {}
        run_id = record.get('run_id')
        if not run_id:
            raise ValueError('Refresh status to obtain a managed run ID')
        question = ('Cancel active work and stop?' if cancel else 'Stop claiming new work and wait for active work?')
        if messagebox.askyesno('Stop runner', question + '\n' + self.project.get() + '\nRun: ' + run_id, parent=self.root):
            self.send('runner.stop', dict(self.config_args(), run_id=run_id, apply=True, cancel_active=cancel))

    def change(self, action):
        args = self.config_args()
        if action == 'resume' and not messagebox.askyesno('Resume new work',
                'An active runner may invoke providers for queued work. Resume?', parent=self.root):
            return
        self.send(action, dict(args, apply=True))

    def limit(self):
        args = self.config_args()
        value = simpledialog.askstring('Call limit', 'Total lifetime call cap (0 stops new calls; blank means unlimited).\n'
                                       'Increasing the limit may allow queued work to run. Usage is not reset.', parent=self.root)
        if value is not None:
            cap = int(value) if value.strip() else None
            if cap is not None and not 0 <= cap <= 2**63 - 1:
                raise ValueError('Enter a nonnegative integer')
            self.send('limit', dict(args, apply=True, max_calls=cap))

    def result(self):
        args = self.config_args()
        task = simpledialog.askstring('Read result', 'Exact task ID:', parent=self.root)
        if task:
            self.send('result', dict(args, task_id=task))

    def clear_tasks(self, note):
        self.task_config = None
        children = self.task_tree.get_children()
        if children:
            self.task_tree.delete(*children)
        self.task_note.set(note)
        self.display(self.status_output, {})

    def refresh_tasks(self):
        if self.worker.busy:
            return
        args = self.config_args()
        self.clear_tasks('Loading recent tasks\u2026')
        self.send('tasks.list', dict(args, limit=20))

    def read_selected_result(self):
        if self.worker.busy:
            return
        args = self.config_args()
        selection = self.task_tree.selection()
        if not selection or self.task_config != args['config']:
            raise ValueError('Refresh tasks and select a task first')
        self.send('result', dict(args, task_id=selection[0]))

    def send(self, action, args):
        self.pending_args = args
        self.worker.submit(action, args)
        self.started = time.monotonic()
        self.notice.set('Working: ' + action)
        self.update_controls()

    def update_controls(self):
        busy = self.worker.busy
        for widget in self.controls:
            widget.configure(state='disabled' if busy else 'normal')
        self.selector.configure(state='disabled' if busy else 'readonly')
        for selector in self.shell_selectors:
            selector.configure(state='disabled' if busy else 'readonly')
        self.read_task_button.configure(state='normal' if not busy and self.task_config
                                        and self.task_tree.selection() else 'disabled')
        self.apply_button.configure(state='normal' if not busy and self.plan and self.plan_valid and time.monotonic() < self.plan_deadline else 'disabled')
        self.cancel_button.configure(state='normal' if not busy and (self.plan or self.discard_plan) else 'disabled')

    def poll(self):
        import queue
        try:
            action, reply, error = self.worker.events.get_nowait()
        except queue.Empty:
            if self.worker.busy and time.monotonic() - self.started > 5:
                self.notice.set('Waiting for core. Closing the window does not cancel accepted changes or stop runners.')
        else:
            self.worker.busy = False
            self.handle_reply(action, reply, error)
        if self.plan and time.monotonic() >= self.plan_deadline:
            self.invalidate_plan()
            self.discard_plan = None
        self.update_controls()
        self.root.after(100, self.poll)

    def handle_reply(self, action, reply, error):
        if error:
            self.invalidate_plan()
            self.status = None
            self.after_cancel = None
            self.notice.set(error)
            self.clear_tasks('Connection unavailable. Reconnect and refresh tasks.')
            return
        if action in ('tasks.list', 'result'):
            selected_config = str(self.selected() / 'bridge.json') if self.project.get() else None
            if (self.pending_args or {}).get('config') != selected_config:
                self.clear_tasks('Project changed. Refresh tasks to see its results.')
                self.notice.set('Ignored a response for a different project.')
                return
        result = reply.get('result') or {}
        target = self.setup_output if action.startswith(('setup.', 'discover')) else self.status_output
        self.display(target, reply)
        self.notice.set('Completed: ' + action if reply['ok'] else
                        (reply['problem']['code'] + ': ' + reply['problem']['next_action']))
        if action == 'discover' and reply['ok']:
            self.connection.set('CLI candidates shown below. Choose an executable explicitly. Authentication is not checked.')
        elif action == 'setup.preview' and reply['ok']:
            self.plan = result['plan_id']
            self.plan_deadline = time.monotonic() + result['expires_in']
            report = result['report']
            self.plan_valid = bool(report.get('ok'))
            self.connection.set('Connection: ' + report['connection'] + '. Review changes below before applying.')
        elif action == 'setup.apply':
            self.status = None
            self.connection.set('Connection: ' + result.get('connection', 'UNKNOWN')
                + '. ' + ('Copy the first request; authenticate in the official CLI and start the runner separately.'
                          if reply['ok'] else 'Review details and any backup paths. Preview again before applying.'))
        elif action == 'setup.cancel':
            pending = getattr(self, 'after_cancel', None)
            self.after_cancel = None
            self.connection.set('Preview discarded. No connection changes applied.')
            if pending and reply['ok']:
                self.send('setup.preview', pending)
        elif action == 'runner.status':
            self.status = result if reply['ok'] else None
            self.status_label.set(status_text(result) if reply['ok'] else 'Status unavailable. Inspect the error below.')
            if reply['ok']:
                self.refresh_tasks()
        elif action == 'tasks.list':
            if reply['ok']:
                self.task_config = self.pending_args['config']
                tasks = result['tasks']
                for task in tasks:
                    self.task_tree.insert('', 'end', iid=task['id'], values=task_values(task))
                if tasks:
                    self.task_tree.selection_set(tasks[0]['id'])
                suffix = ' More tasks are available by ID.' if result.get('more') else ''
                self.task_note.set(('Recent tasks: ' + str(len(tasks)) if tasks else 'No tasks in this project yet.') + suffix)
            else:
                self.task_note.set('Task list unavailable. Inspect the error or use Read by ID.')
        elif action in ('runner.start', 'runner.stop', 'pause', 'resume', 'limit'):
            self.status = None
            self.status_label.set('Operation response shown below. Refresh status before the next runner operation.')

    def reconnect(self):
        self.invalidate_plan()
        self.worker.reconnect()
        self.discard_plan = None
        self.clear_tasks('Core reconnected. Refresh tasks before reading results.')
        self.notice.set('Core connection reset. Preview again; refresh to inspect accepted changes.')

    def close(self):
        self.worker.close()
        self.root.destroy()


def main():
    parser = argparse.ArgumentParser(description='PatchPort local management window')
    parser.add_argument('--core', type=Path, help='Explicit installed core Python (development only)')
    parser.add_argument('--source', type=Path, help='Explicit core source directory (development only)')
    parser.add_argument('--registry', type=Path, help='Project registry path')
    parser.add_argument('--project', type=Path, help='Open an existing project without connecting or starting work')
    args = parser.parse_args()
    if args.project is not None:
        args.project = args.project.expanduser().resolve()
        if not args.project.is_dir():
            parser.error('--project must be an existing directory')
    root = tk.Tk()
    try:
        if getattr(sys, 'frozen', False) and (args.core or args.source):
            raise ValueError('Packaged applications use their bundled core')
        core = args.core or bundled_core(Path(sys.executable).resolve().parent.parent)
        registry = args.registry or Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'PatchPort/projects.json'
        app = Application(root, Worker(lambda: CoreClient(core, source=args.source)), Projects(registry))
        if args.project is not None:
            app.select_project(args.project)
            if (args.project / 'bridge.json').is_file():
                app.tabs.select(app.manage_tab)
                app.refresh()
    except (OSError, ValueError, KeyError) as exc:
        messagebox.showerror('PatchPort startup', str(exc), parent=root)
        root.destroy()
        return 1
    root.mainloop()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
