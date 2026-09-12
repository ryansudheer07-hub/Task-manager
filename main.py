import json
import os
from functools import wraps

from dotenv import load_dotenv
from werkzeug.security import generate_password_hash, check_password_hash

from flask import (
    Flask, render_template, request, redirect, session, url_for, flash, abort, jsonify
)
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask_wtf.csrf import CSRFProtect
from sqlalchemy.pool import NullPool
from datetime import datetime, timedelta

import estimation
import reminders
import scheduling

load_dotenv()

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL')
# Supabase runs its own connection pooler, so a second pool in this process
# would hold connections the pooler expects to manage.
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {'poolclass': NullPool}
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY')

if not app.config['SQLALCHEMY_DATABASE_URI']:
    raise RuntimeError('DATABASE_URL is not set. Copy .env.example to .env and fill it in.')
if not app.config['SECRET_KEY']:
    raise RuntimeError('SECRET_KEY is not set. Copy .env.example to .env and fill it in.')

db = SQLAlchemy(app)
migrate = Migrate(app, db)

# Signs CSRF tokens with SECRET_KEY. Only state-changing methods are
# protected, so the GET polling endpoint is unaffected. A fetch that posts
# would need the token in an X-CSRFToken header rather than a form field.
csrf = CSRFProtect(app)

# Allowed values for Todo.status.
TODO_STATUSES = ('pending', 'scheduled', 'in_progress', 'completed')

# How recently an identical task title counts as the same submission rather
# than a genuine second task.
DUPLICATE_WINDOW_SECONDS = 10


def login_required(view):
    """Redirect anonymous visitors to the login page."""
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if not session.get('user_id'):
            return redirect(url_for('login'))
        return view(*args, **kwargs)
    return wrapped_view


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    # Nullable because accounts created before this column existed have no email.
    email = db.Column(db.String(255), unique=True, nullable=True)
    timezone = db.Column(db.String(64), nullable=False, server_default='UTC', default='UTC')
    email_verified = db.Column(db.Boolean, nullable=False, server_default='0', default=False)
    work_start_hour = db.Column(db.Integer, nullable=False, server_default='9', default=9)
    work_end_hour = db.Column(db.Integer, nullable=False, server_default='17', default=17)

    todos = db.relationship('Todo', back_populates='user')

    def __repr__(self):
        return f'<User {self.username}>'


class Todo(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    content = db.Column(db.String(200), nullable=False)
    date_created = db.Column(db.DateTime, default=datetime.utcnow)
    completed = db.Column(db.Boolean, default=False)
    # Nullable because tasks created before per-user ownership have no owner.
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True, index=True)
    estimated_minutes = db.Column(db.Integer, nullable=True)
    status = db.Column(db.String(20), nullable=False, server_default='pending', default='pending')
    scheduled_start = db.Column(db.DateTime, nullable=True)
    scheduled_end = db.Column(db.DateTime, nullable=True)
    actual_start = db.Column(db.DateTime, nullable=True)
    actual_end = db.Column(db.DateTime, nullable=True)
    priority = db.Column(db.Integer, nullable=False, server_default='2', default=2)
    deadline = db.Column(db.DateTime, nullable=True)
    # AI-suggested breakdown, stored as a JSON array of strings.
    subtasks_json = db.Column(db.Text, nullable=True)
    # Snapshotted at completion from the task's time sessions, so accuracy
    # history stays queryable without walking child rows and survives any
    # later pruning of session data.
    actual_minutes = db.Column(db.Float, nullable=True)
    accuracy_ratio = db.Column(db.Float, nullable=True)

    @property
    def subtasks(self):
        """The suggested breakdown, or an empty list if there is none."""
        if not self.subtasks_json:
            return []
        try:
            value = json.loads(self.subtasks_json)
        except ValueError:
            return []
        return value if isinstance(value, list) else []

    @subtasks.setter
    def subtasks(self, values):
        self.subtasks_json = json.dumps(list(values)) if values else None

    @property
    def display_status(self):
        """Status for presentation, which adds 'overrun' to the stored values.

        Overrun is derived rather than stored: a task is overrunning once the
        time logged against it exceeds its estimate while it is still open.
        """
        if self.status != 'completed' and self.estimated_minutes:
            # Uses elapsed rather than logged time so a task that is running
            # over right now is flagged immediately, not only once stopped.
            if self.elapsed_seconds() / 60 > self.estimated_minutes:
                return 'overrun'
        return self.status

    def logged_minutes(self):
        """Total minutes recorded across every finished work session."""
        total = 0.0
        for entry in self.time_sessions:
            if entry.started_at and entry.ended_at:
                total += (entry.ended_at - entry.started_at).total_seconds() / 60
        return total

    def open_session(self):
        """The currently running session, or None when the task is not running."""
        for entry in self.time_sessions:
            if entry.ended_at is None:
                return entry
        return None

    def elapsed_seconds(self):
        """Seconds worked so far, including any session still running.

        The live counter in the browser starts from this value and ticks
        upwards, so a page reload never loses time already logged.
        """
        total = self.logged_minutes() * 60
        running = self.open_session()
        if running and running.started_at:
            total += (datetime.utcnow() - running.started_at).total_seconds()
        return int(total)

    user = db.relationship('User', back_populates='todos')
    time_sessions = db.relationship(
        'TimeSession', back_populates='todo', cascade='all, delete-orphan'
    )
    reminders = db.relationship(
        'Reminder', back_populates='todo', cascade='all, delete-orphan'
    )

    def __repr__(self):
        return f'<Task {self.id}>'


class TimeSession(db.Model):
    """One continuous stretch of work on a task. A task may have several."""
    id = db.Column(db.Integer, primary_key=True)
    todo_id = db.Column(db.Integer, db.ForeignKey('todo.id'), nullable=False, index=True)
    started_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    ended_at = db.Column(db.DateTime, nullable=True)

    todo = db.relationship('Todo', back_populates='time_sessions')

    def __repr__(self):
        return f'<TimeSession {self.id} todo={self.todo_id}>'


class Reminder(db.Model):
    """Record of a notification already sent, so nothing is sent twice."""
    id = db.Column(db.Integer, primary_key=True)
    todo_id = db.Column(db.Integer, db.ForeignKey('todo.id'), nullable=False, index=True)
    reminder_type = db.Column(db.String(20), nullable=False)
    sent_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    # One reminder of each type per task. Enforced in the database so two
    # concurrent scheduler runs cannot both pass a check and both send.
    __table_args__ = (
        db.UniqueConstraint('todo_id', 'reminder_type', name='uq_reminder_todo_type'),
    )

    todo = db.relationship('Todo', back_populates='reminders')

    def __repr__(self):
        return f'<Reminder {self.reminder_type} todo={self.todo_id}>'


@app.context_processor
def inject_template_globals():
    """Values every template needs: input bounds and the client poll interval."""
    return {
        'min_minutes': estimation.MIN_MINUTES,
        'max_minutes': estimation.MAX_MINUTES,
        'reminder_poll_seconds': reminders.POLL_INTERVAL_SECONDS,
    }


def average_accuracy_ratio(user_id, limit=20):
    """Average of actual/estimated minutes over a user's recent completed tasks.

    Returns None when the user has no completed task with both a usable
    estimate and logged time, so callers can skip calibration entirely.
    """
    ratios = [
        row.accuracy_ratio
        for row in (
            Todo.query
            .filter(
                Todo.user_id == user_id,
                Todo.status == 'completed',
                Todo.accuracy_ratio.isnot(None),
            )
            .order_by(Todo.actual_end.desc())
            .limit(limit)
            .all()
        )
    ]

    if not ratios:
        return None
    return sum(ratios) / len(ratios)


def recent_completed_examples(user_id, limit=3):
    """Recent finished tasks with both an estimate and a measured duration.

    Used as few-shot examples so the model can anchor on what this user's
    tasks actually cost.
    """
    return (
        Todo.query
        .filter(
            Todo.user_id == user_id,
            Todo.status == 'completed',
            Todo.accuracy_ratio.isnot(None),
        )
        .order_by(Todo.actual_end.desc())
        .limit(limit)
        .all()
    )


@app.route('/register', methods=['POST', 'GET'])
def register():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()

        if not username or not password:
            flash('Username and password are both required.')
            return render_template('register.html')

        if User.query.filter_by(username=username).first():
            flash('That username is already taken. Please choose another.')
            return render_template('register.html')

        user = User(username=username, password_hash=generate_password_hash(password))
        try:
            db.session.add(user)
            db.session.commit()
            return redirect(url_for('login'))
        except Exception:
            db.session.rollback()
            flash('There was an issue creating your account.')
            return render_template('register.html')

    return render_template('register.html')


@app.route('/login', methods=['POST', 'GET'])
def login():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        user = User.query.filter_by(username=username).first()
        if user and check_password_hash(user.password_hash, password):
            session['user_id'] = user.id
            return redirect(url_for('home'))
        flash('Invalid username or password.')
        return render_template('login.html')
    return render_template('login.html')


@app.route('/logout')
def logout():
    session.pop('user_id', None)
    return redirect(url_for('login'))


@app.route('/about')
def about():
    return render_template('about.html')


@app.route('/', methods=['POST', 'GET'])
@login_required
def home():
    user_id = session['user_id']

    if request.method == 'POST':
        task_content = request.form.get('content', '').strip()
        if not task_content:
            return redirect(url_for('home'))

        # A resubmitted form, a refresh or an impatient second click would
        # otherwise create the same task twice, and the slow estimate call
        # keeps that window open for seconds. Treat an identical title from
        # the same user moments ago as the same submission.
        recent = Todo.query.filter(
            Todo.user_id == user_id,
            Todo.content == task_content,
            Todo.date_created >= datetime.utcnow() - timedelta(seconds=DUPLICATE_WINDOW_SECONDS),
        ).order_by(Todo.date_created.desc()).first()

        if recent:
            return redirect(url_for('confirm_estimate', id=recent.id))

        # Estimation runs before the insert but can never prevent it: on any
        # failure estimate_task returns the fallback and an error string.
        ratio = average_accuracy_ratio(user_id)
        examples = [
            (row.content, row.estimated_minutes, row.actual_minutes)
            for row in recent_completed_examples(user_id)
        ]
        minutes, subtasks, error = estimation.estimate_task(task_content, ratio, examples)

        new_task = Todo(content=task_content, user_id=user_id, estimated_minutes=minutes)
        new_task.subtasks = subtasks

        try:
            db.session.add(new_task)
            db.session.commit()
        except Exception:
            db.session.rollback()
            flash('There was an issue adding your task.')
            return redirect(url_for('home'))

        if error:
            app.logger.warning('Estimation failed for task %s: %s', new_task.id, error)
            flash(f'Saved with a default {minutes} minute estimate; AI estimate unavailable.')

        # Send the user to the confirm step so the estimate can be adjusted.
        return redirect(url_for('confirm_estimate', id=new_task.id))

    tasks = (
        Todo.query
        .filter(Todo.user_id == user_id)
        .order_by(Todo.date_created)
        .all()
    )
    return render_template(
        'index.html',
        tasks=tasks,
        accuracy_ratio=average_accuracy_ratio(user_id),
    )


@app.route('/confirm/<int:id>', methods=['GET', 'POST'])
@login_required
def confirm_estimate(id):
    """Review step where the AI estimate can be edited before it is kept."""
    task = Todo.query.get_or_404(id)
    if task.user_id != session['user_id']:
        abort(404)

    if request.method == 'POST':
        raw = request.form.get('estimated_minutes', '').strip()
        try:
            minutes = int(raw)
            if not estimation.MIN_MINUTES <= minutes <= estimation.MAX_MINUTES:
                raise ValueError
        except ValueError:
            flash(
                f'Enter a whole number of minutes between '
                f'{estimation.MIN_MINUTES} and {estimation.MAX_MINUTES}.'
            )
            return render_template('confirm.html', task=task)

        task.estimated_minutes = minutes
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            flash('There was an issue saving the estimate.')
        return redirect(url_for('home'))

    return render_template('confirm.html', task=task)


def owned_task_or_404(id):
    """Fetch a task belonging to the logged-in user, or 404.

    Returning 404 rather than 403 avoids revealing that another user's task
    with this id exists.
    """
    task = Todo.query.get_or_404(id)
    if task.user_id != session.get('user_id'):
        abort(404)
    return task


@app.route('/api/reminders/check', methods=['GET'])
def check_reminders():
    """Report reminders the browser should raise, and record them as sent.

    Anything returned here has its Reminder row written before the response
    leaves, so a later poll never repeats it. Returns JSON rather than a
    redirect when signed out, because the caller is fetch, not a browser
    following links.
    """
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'error': 'not authenticated'}), 401

    try:
        overruns = reminders.find_overruns(db, Todo, Reminder, user_id)
        due_to_start = reminders.find_due_to_start(db, Todo, Reminder, user_id)
        stale = reminders.find_stale(db, Todo, Reminder, user_id)
    except Exception:
        db.session.rollback()
        app.logger.exception('Reminder check failed for user %s', user_id)
        # An empty result keeps the client polling without raising a
        # notification it cannot substantiate.
        return jsonify({'overruns': [], 'due_to_start': [], 'stale': []})

    return jsonify({
        'overruns': overruns,
        'due_to_start': due_to_start,
        'stale': stale,
    })


@app.route('/schedule', methods=['GET', 'POST'])
@login_required
def schedule_day():
    """Plan the day, and show the resulting timeline."""
    user = db.session.get(User, session['user_id'])

    if request.method == 'POST':
        work_start, work_end = scheduling.working_window(user)

        candidates = (
            Todo.query
            .filter(
                Todo.user_id == user.id,
                Todo.status.in_(('pending', 'scheduled')),
            )
            .order_by(Todo.priority, Todo.deadline)
            .all()
        )

        blocks, error = scheduling.build_schedule(candidates, work_start, work_end)

        if error:
            app.logger.warning('Scheduling failed for user %s: %s', user.id, error)
            flash('Could not build a schedule right now; your tasks are unchanged.')
            return redirect(url_for('schedule_day'))

        placed = {todo_id: (start, end) for todo_id, start, end in blocks}

        try:
            for task in candidates:
                if task.id in placed:
                    task.scheduled_start, task.scheduled_end = placed[task.id]
                    task.status = 'scheduled'
                else:
                    # Overflow stays visible as unscheduled rather than vanishing.
                    task.scheduled_start = None
                    task.scheduled_end = None
                    task.status = 'pending'
            db.session.commit()
        except Exception:
            db.session.rollback()
            flash('There was a problem saving the schedule.')
            return redirect(url_for('schedule_day'))

        overflow = len(candidates) - len(placed)
        if overflow > 0:
            flash(
                f'{len(placed)} task(s) scheduled. {overflow} did not fit in your '
                'working hours and remain unscheduled.'
            )

        return redirect(url_for('schedule_day'))

    scheduled = (
        Todo.query
        .filter(Todo.user_id == user.id, Todo.scheduled_start.isnot(None))
        .order_by(Todo.scheduled_start)
        .all()
    )
    unscheduled = (
        Todo.query
        .filter(
            Todo.user_id == user.id,
            Todo.scheduled_start.is_(None),
            Todo.status != 'completed',
        )
        .order_by(Todo.priority, Todo.date_created)
        .all()
    )

    work_start, work_end = scheduling.working_window(user)

    return render_template(
        'schedule.html',
        scheduled=scheduled,
        unscheduled=unscheduled,
        user=user,
        work_start=work_start,
        work_end=work_end,
    )


@app.route('/reschedule/<int:id>', methods=['POST'])
@login_required
def reschedule(id):
    """Manually override the times the scheduler chose for one task."""
    task = owned_task_or_404(id)

    raw_start = request.form.get('scheduled_start', '').strip()
    raw_end = request.form.get('scheduled_end', '').strip()

    if not raw_start or not raw_end:
        # Clearing both fields removes the task from the timeline.
        task.scheduled_start = None
        task.scheduled_end = None
        if task.status == 'scheduled':
            task.status = 'pending'
    else:
        try:
            # datetime-local inputs submit as YYYY-MM-DDTHH:MM.
            start = datetime.fromisoformat(raw_start)
            end = datetime.fromisoformat(raw_end)
        except ValueError:
            flash('Enter valid start and end times.')
            return redirect(url_for('schedule_day'))

        if end <= start:
            flash('The end time must be after the start time.')
            return redirect(url_for('schedule_day'))

        task.scheduled_start = start
        task.scheduled_end = end
        if task.status == 'pending':
            task.status = 'scheduled'

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        flash('There was a problem saving those times.')

    return redirect(url_for('schedule_day'))


@app.route('/delete/<int:id>', methods=['POST'])
@login_required
def delete(id):
    task_to_delete = owned_task_or_404(id)
    try:
        db.session.delete(task_to_delete)
        db.session.commit()
        return redirect(url_for('home'))
    except Exception:
        db.session.rollback()
        return 'We are facing a problem'


@app.route('/update/<int:id>', methods=['GET', 'POST'])
@login_required
def update(id):
    task_to_update = owned_task_or_404(id)
    if request.method == 'POST':
        task_to_update.content = request.form.get('content', '').strip() or task_to_update.content

        raw_minutes = request.form.get('estimated_minutes', '').strip()
        if raw_minutes:
            try:
                minutes = int(raw_minutes)
                if not estimation.MIN_MINUTES <= minutes <= estimation.MAX_MINUTES:
                    raise ValueError
                task_to_update.estimated_minutes = minutes
            except ValueError:
                flash(
                    f'Enter a whole number of minutes between '
                    f'{estimation.MIN_MINUTES} and {estimation.MAX_MINUTES}.'
                )
                return render_template('update.html', task=task_to_update)

        try:
            db.session.commit()
            return redirect(url_for('home'))
        except Exception:
            db.session.rollback()
            return 'There was an issue'
    return render_template('update.html', task=task_to_update)


@app.route('/start/<int:id>', methods=['POST'])
@login_required
def start(id):
    """Begin working on a task, opening a new time session."""
    task = owned_task_or_404(id)

    if task.status == 'completed':
        flash('That task is already complete.')
        return redirect(url_for('home'))

    now = datetime.utcnow()

    try:
        # An already-open session means the task is running; do not stack another.
        if not task.open_session():
            db.session.add(TimeSession(todo_id=task.id, started_at=now))

        task.status = 'in_progress'
        if task.actual_start is None:
            task.actual_start = now

        db.session.commit()
    except Exception:
        db.session.rollback()
        flash('There was a problem starting the task.')

    return redirect(url_for('home'))


@app.route('/complete/<int:id>', methods=['POST'])
@login_required
def complete(id):
    """Finish a task, closing any open session and recording accuracy."""
    task = owned_task_or_404(id)
    now = datetime.utcnow()

    try:
        for entry in task.time_sessions:
            if entry.ended_at is None:
                entry.ended_at = now

        task.completed = True
        task.status = 'completed'
        task.actual_end = now

        # Snapshot the outcome so later estimates can be calibrated against it.
        task.actual_minutes = task.logged_minutes()
        if task.estimated_minutes and task.actual_minutes > 0:
            task.accuracy_ratio = task.actual_minutes / task.estimated_minutes

        db.session.commit()
        return redirect(url_for('home'))
    except Exception:
        db.session.rollback()
        return 'There was a problem completing the task'


if __name__ == '__main__':
    # Schema is owned by Flask-Migrate. Run `flask db upgrade` to create or
    # update tables; do not call db.create_all() here.
    app.run(debug=os.environ.get('FLASK_DEBUG', 'false') == 'true')
