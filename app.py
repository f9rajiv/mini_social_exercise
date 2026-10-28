from flask import Flask, render_template, request, redirect, url_for, session, flash, g, abort
from werkzeug.security import generate_password_hash, check_password_hash
from cryptography.fernet import Fernet
import collections
import json
import sqlite3
import hashlib
import math
import re
from datetime import datetime, timedelta
from urllib.parse import urlparse

app = Flask(__name__)
app.secret_key = '123456789' 
DATABASE = 'database.sqlite'

# Load censorship data
# WARNING! The censorship.dat file contains disturbing language when decrypted. 
# If you want to test whether moderation works, 
# you can trigger censorship using these words: 
# tier1badword, tier2badword, tier3badword
ENCRYPTED_FILE_PATH = 'censorship.dat'
fernet = Fernet('xpplx11wZUibz0E8tV8Z9mf-wwggzSrc21uQ17Qq2gg=')
with open(ENCRYPTED_FILE_PATH, 'rb') as encrypted_file:
    encrypted_data = encrypted_file.read()
decrypted_data = fernet.decrypt(encrypted_data)
MODERATION_CONFIG = json.loads(decrypted_data)
TIER1_WORDS = MODERATION_CONFIG['categories']['tier1_severe_violations']['words']
TIER2_PHRASES = MODERATION_CONFIG['categories']['tier2_spam_scams']['phrases']
TIER3_WORDS = MODERATION_CONFIG['categories']['tier3_mild_profanity']['words']

def get_db():
    """
    Connect to the application's configured database. The connection
    is unique for each request and will be reused if this is called
    again.
    """
    if 'db' not in g:
        g.db = sqlite3.connect(
            DATABASE,
            detect_types=sqlite3.PARSE_DECLTYPES
        )
        g.db.row_factory = sqlite3.Row

    return g.db


@app.teardown_appcontext
def close_connection(exception):
    """Closes the database again at the end of the request."""
    db = g.pop('db', None)

    if db is not None:
        db.close()


def query_db(query, args=(), one=False, commit=False):
    """
    Queries the database and returns a list of dictionaries, a single
    dictionary, or None. Also handles write operations.
    """
    db = get_db()
    
    # Using 'with' on a connection object implicitly handles transactions.
    # The 'with' statement will automatically commit if successful, 
    # or rollback if an exception occurs. This is safer.
    try:
        with db:
            cur = db.execute(query, args)
        
        # For SELECT statements, fetch the results after the transaction block
        if not commit:
            rv = cur.fetchall()
            return (rv[0] if rv else None) if one else rv
        
        # For write operations, we might want the cursor to get info like lastrowid
        return cur

    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return None


def init_assignment1_tables():
    """
    Creates the database tables needed by the Coding Assignment #1 features.
    Safe to run on every start: tables are only created if they are missing.
    """
    db = sqlite3.connect(DATABASE)
    db.executescript("""
        -- Chapter 2, Design Claim 13: one weekly contribution goal per user
        CREATE TABLE IF NOT EXISTS contribution_goals (
            user_id         INTEGER PRIMARY KEY,
            posts_target    INTEGER NOT NULL,
            comments_target INTEGER NOT NULL,
            set_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL
        );

        -- Chapter 3, Design Claim 6: named interest groups that users create and join
        CREATE TABLE IF NOT EXISTS interest_groups (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL UNIQUE COLLATE NOCASE,
            description TEXT,
            creator_id  INTEGER NOT NULL,
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL
        );
        CREATE TABLE IF NOT EXISTS group_members (
            group_id  INTEGER NOT NULL,
            user_id   INTEGER NOT NULL,
            joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
            PRIMARY KEY (group_id, user_id)
        );
    """)
    # Posts can optionally belong to a group. Existing posts keep group_id = NULL.
    post_columns = [row[1] for row in db.execute('PRAGMA table_info(posts)')]
    if 'group_id' not in post_columns:
        db.execute('ALTER TABLE posts ADD COLUMN group_id INTEGER')
    db.commit()
    db.close()

init_assignment1_tables()


def init_assignment3_tables():
    """
    Database change for Coding Assignment #3 (safe to run on every start).
    Chapter 5, Design Claim 19: a post can be marked as the author's introduction.
    """
    db = sqlite3.connect(DATABASE)
    post_columns = [row[1] for row in db.execute('PRAGMA table_info(posts)')]
    if 'is_intro' not in post_columns:
        db.execute('ALTER TABLE posts ADD COLUMN is_intro INTEGER NOT NULL DEFAULT 0')
    db.commit()
    db.close()

init_assignment3_tables()

@app.template_filter('datetimeformat')
def datetimeformat(value):
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        dt = datetime.strptime(value, '%Y-%m-%d %H:%M:%S')
    else:
        return "N/A"
    return dt.strftime('%b %d, %Y %H:%M')

REACTION_EMOJIS = {
    'like': '❤️', 'love': '😍', 'laugh': '😂',
    'wow': '😮', 'sad': '😢', 'angry': '😠',
}
REACTION_TYPES = list(REACTION_EMOJIS.keys())


@app.route('/')
def feed():
    #  1. Get Pagination and Filter Parameters 
    try:
        page = int(request.args.get('page', 1))
    except ValueError:
        page = 1
    sort = request.args.get('sort', 'new').lower()
    show = request.args.get('show', 'all').lower()
    
    # Define how many posts to show per page
    POSTS_PER_PAGE = 10
    offset = (page - 1) * POSTS_PER_PAGE

    current_user_id = session.get('user_id')
    params = []

    #  2. Build the Query 
    where_clause = ""
    if show == 'following' and current_user_id:
        where_clause = "WHERE p.user_id IN (SELECT followed_id FROM follows WHERE follower_id = ?)"
        params.append(current_user_id)

    # Add the pagination parameters to the query arguments
    pagination_params = (POSTS_PER_PAGE, offset)

    if sort == 'popular':
        query = f"""
            SELECT p.id, p.content, p.created_at, u.username, u.id as user_id,
                   IFNULL(r.total_reactions, 0) as total_reactions,
                   ig.id as group_id, ig.name as group_name, p.is_intro
            FROM posts p
            JOIN users u ON p.user_id = u.id
            LEFT JOIN interest_groups ig ON p.group_id = ig.id
            LEFT JOIN (
                SELECT post_id, COUNT(*) as total_reactions FROM reactions GROUP BY post_id
            ) r ON p.id = r.post_id
            {where_clause}
            ORDER BY total_reactions DESC, p.created_at DESC
            LIMIT ? OFFSET ?
        """
        final_params = params + list(pagination_params)
        posts = query_db(query, final_params)
    elif sort == 'recommended':
        posts = recommend(current_user_id, show == 'following' and current_user_id)
    else:  # Default sort is 'new'
        query = f"""
            SELECT p.id, p.content, p.created_at, u.username, u.id as user_id,
                   ig.id as group_id, ig.name as group_name, p.is_intro
            FROM posts p
            JOIN users u ON p.user_id = u.id
            LEFT JOIN interest_groups ig ON p.group_id = ig.id
            {where_clause}
            ORDER BY p.created_at DESC
            LIMIT ? OFFSET ?
        """
        final_params = params + list(pagination_params)
        posts = query_db(query, final_params)

    posts_data = []
    for post in posts:
        # Determine if the current user follows the poster
        followed_poster = False
        if current_user_id and post['user_id'] != current_user_id:
            follow_check = query_db(
                'SELECT 1 FROM follows WHERE follower_id = ? AND followed_id = ?',
                (current_user_id, post['user_id']),
                one=True
            )
            if follow_check:
                followed_poster = True

        # Determine if the current user reacted to this post and with what reaction
        user_reaction = None
        if current_user_id:
            reaction_check = query_db(
                'SELECT reaction_type FROM reactions WHERE user_id = ? AND post_id = ?',
                (current_user_id, post['id']),
                one=True
            )
            if reaction_check:
                user_reaction = reaction_check['reaction_type']

        reactions = query_db('SELECT reaction_type, COUNT(*) as count FROM reactions WHERE post_id = ? GROUP BY reaction_type', (post['id'],))
        comments_raw = query_db('SELECT c.id, c.content, c.created_at, u.username, u.id as user_id FROM comments c JOIN users u ON c.user_id = u.id WHERE c.post_id = ? ORDER BY c.created_at ASC', (post['id'],))
        post_dict = dict(post)
        post_dict['content'], _ = moderate_content(post_dict['content'])
        comments_moderated = []
        for comment in comments_raw:
            comment_dict = dict(comment)
            comment_dict['content'], _ = moderate_content(comment_dict['content'])
            comments_moderated.append(comment_dict)
        posts_data.append({
            'post': post_dict,
            'reactions': reactions,
            'user_reaction': user_reaction,
            'followed_poster': followed_poster,
            'comments': comments_moderated
        })

    #  3. Weekly goal widget (Chapter 2, Design Claims 13 & 15)
    # Logged-in users see their progress, or a suggested goal if they have none yet.
    goal_progress = get_goal_progress(current_user_id) if current_user_id else None
    suggested_goal = suggest_goal(current_user_id) if current_user_id and not goal_progress else None

    #  Newcomer card (Chapter 5, Design Claims 19 & 25): intro prompt and access progress
    newcomer = newcomer_status(current_user_id) if current_user_id else None

    #  4. Render Template with Pagination Info
    return render_template('feed.html.j2',
                           posts=posts_data,
                           current_sort=sort,
                           current_show=show,
                           page=page, # Pass current page number
                           per_page=POSTS_PER_PAGE, # Pass items per page
                           reaction_emojis=REACTION_EMOJIS,
                           reaction_types=REACTION_TYPES,
                           goal_progress=goal_progress,
                           suggested_goal=suggested_goal,
                           newcomer=newcomer)

@app.route('/posts/new', methods=['POST'])
def add_post():
    """Handles creating a new post from the feed."""
    user_id = session.get('user_id')

    # Block access if user is not logged in
    if not user_id:
        flash('You must be logged in to create a post.', 'danger')
        return redirect(url_for('login'))

    # Get content from the submitted form
    content = request.form.get('content')

    # Optional: the post is made inside an interest group (Chapter 3, Design Claim 6).
    # Only members of the group are allowed to post there.
    group_id = request.form.get('group_id', type=int)
    if group_id and not is_group_member(group_id, user_id):
        flash('You need to join this group before posting in it.', 'warning')
        return redirect(url_for('group_detail', group_id=group_id))

    # Chapter 5, Design Claim 19: the post is the user's introduction (only one allowed)
    is_intro = 1 if request.form.get('is_intro') == '1' and not get_intro_post(user_id) else 0

    # Chapter 5, Design Claim 25: newcomers can only post a few times per day
    status = newcomer_status(user_id)
    if status['is_newcomer'] and status['posts_today'] >= NEWCOMER_DAILY_POSTS:
        flash(f'New members can make {NEWCOMER_DAILY_POSTS} posts per day. '
              f'Comment on other posts to unlock full access sooner!', 'warning')
        return redirect(url_for('group_detail', group_id=group_id) if group_id else url_for('feed'))

    # The raw text is stored; moderate_content() is applied when it is displayed
    moderated_content = content

    # Chapter 4, Design Claim 20: remind the user of the rules before a
    # post that breaks them goes live (they can edit or post anyway)
    if moderated_content and moderated_content.strip():
        reminder = posting_reminder(content, 'post', url_for('add_post'),
                                    {'group_id': group_id or '', 'is_intro': is_intro})
        if reminder:
            return reminder

    # Basic validation to ensure post is not empty
    if moderated_content and moderated_content.strip():
        db = get_db()
        db.execute('INSERT INTO posts (user_id, content, group_id, is_intro) VALUES (?, ?, ?, ?)',
                   (user_id, moderated_content, group_id, is_intro))
        db.commit()
        if is_intro:
            flash('Thanks for introducing yourself! Other members can now welcome you.', 'success')
        else:
            flash('Your post was successfully created!', 'success')
        flash_goal_feedback(user_id)
    else:
        # This will catch empty posts or posts that were fully censored
        flash('Post cannot be empty or was fully censored.', 'warning')

    # Group posts return to the group page, other posts to the main feed
    if group_id:
        return redirect(url_for('group_detail', group_id=group_id))
    return redirect(url_for('feed'))
    
    
@app.route('/posts/<int:post_id>/delete', methods=['POST'])
def delete_post(post_id):
    """Handles deleting a post."""
    user_id = session.get('user_id')

    # Block access if user is not logged in
    if not user_id:
        flash('You must be logged in to delete a post.', 'danger')
        return redirect(url_for('login'))

    # Find the post in the database
    post = query_db('SELECT id, user_id FROM posts WHERE id = ?', (post_id,), one=True)

    # Check if the post exists and if the current user is the owner
    if not post:
        flash('Post not found.', 'danger')
        return redirect(url_for('feed'))

    if post['user_id'] != user_id:
        # Security check: prevent users from deleting others' posts
        flash('You do not have permission to delete this post.', 'danger')
        return redirect(url_for('feed'))

    # If all checks pass, proceed with deletion
    db = get_db()
    # To maintain database integrity, delete associated records first
    db.execute('DELETE FROM comments WHERE post_id = ?', (post_id,))
    db.execute('DELETE FROM reactions WHERE post_id = ?', (post_id,))
    # Finally, delete the post itself
    db.execute('DELETE FROM posts WHERE id = ?', (post_id,))
    db.commit()

    flash('Your post was successfully deleted.', 'success')
    # Redirect back to the page the user came from, or the feed as a fallback
    return redirect(request.referrer or url_for('feed'))

@app.route('/u/<username>')
def user_profile(username):
    """Displays a user's profile page with moderated bio, posts, and latest comments."""
    
    user_raw = query_db('SELECT * FROM users WHERE username = ?', (username,), one=True)
    if not user_raw:
        abort(404)

    user = dict(user_raw)
    moderated_bio, _ = moderate_content(user.get('profile', ''))
    user['profile'] = moderated_bio

    posts_raw = query_db('SELECT id, content, user_id, created_at FROM posts WHERE user_id = ? ORDER BY created_at DESC', (user['id'],))
    posts = []
    for post_raw in posts_raw:
        post = dict(post_raw)
        moderated_post_content, _ = moderate_content(post['content'])
        post['content'] = moderated_post_content
        posts.append(post)

    comments_raw = query_db('SELECT id, content, user_id, post_id, created_at FROM comments WHERE user_id = ? ORDER BY created_at DESC LIMIT 100', (user['id'],))
    comments = []
    for comment_raw in comments_raw:
        comment = dict(comment_raw)
        moderated_comment_content, _ = moderate_content(comment['content'])
        comment['content'] = moderated_comment_content
        comments.append(comment)

    followers_count = query_db('SELECT COUNT(*) as cnt FROM follows WHERE followed_id = ?', (user['id'],), one=True)['cnt']
    following_count = query_db('SELECT COUNT(*) as cnt FROM follows WHERE follower_id = ?', (user['id'],), one=True)['cnt']

    #  NEW: CHECK FOLLOW STATUS 
    is_currently_following = False # Default to False
    current_user_id = session.get('user_id')
    
    # We only need to check if a user is logged in
    if current_user_id:
        follow_relation = query_db(
            'SELECT 1 FROM follows WHERE follower_id = ? AND followed_id = ?',
            (current_user_id, user['id']),
            one=True
        )
        if follow_relation:
            is_currently_following = True
    # --

    # Groups the user belongs to (Chapter 3, Design Claim 6)
    user_groups = query_db('''
        SELECT ig.id, ig.name FROM interest_groups ig
        JOIN group_members gm ON gm.group_id = ig.id
        WHERE gm.user_id = ? ORDER BY ig.name
    ''', (user['id'],))

    # Chapter 5, Design Claim 19: the user's introduction is pinned on their profile
    intro_post = get_intro_post(user['id'])
    if intro_post:
        intro_post = dict(intro_post)
        intro_post['content'], _ = moderate_content(intro_post['content'])

    return render_template('user_profile.html.j2',
                           user=user,
                           posts=posts,
                           comments=comments,
                           followers_count=followers_count,
                           following_count=following_count,
                           is_following=is_currently_following,
                           user_groups=user_groups,
                           intro_post=intro_post)
    

@app.route('/u/<username>/followers')
def user_followers(username):
    user = query_db('SELECT * FROM users WHERE username = ?', (username,), one=True)
    if not user:
        abort(404)
    followers = query_db('''
        SELECT u.username
        FROM follows f
        JOIN users u ON f.follower_id = u.id
        WHERE f.followed_id = ?
    ''', (user['id'],))
    return render_template('user_list.html.j2', user=user, users=followers, title="Followers of")

@app.route('/u/<username>/following')
def user_following(username):
    user = query_db('SELECT * FROM users WHERE username = ?', (username,), one=True)
    if not user:
        abort(404)
    following = query_db('''
        SELECT u.username
        FROM follows f
        JOIN users u ON f.followed_id = u.id
        WHERE f.follower_id = ?
    ''', (user['id'],))
    return render_template('user_list.html.j2', user=user, users=following, title="Users followed by")

@app.route('/posts/<int:post_id>')
def post_detail(post_id):
    """Displays a single post and its comments, with content moderation applied."""
    
    post_raw = query_db('''
        SELECT p.id, p.content, p.created_at, u.username, u.id as user_id
        FROM posts p
        JOIN users u ON p.user_id = u.id
        WHERE p.id = ?
    ''', (post_id,), one=True)

    if not post_raw:
        # The abort function will stop the request and show a 404 Not Found page.
        abort(404)

    #  Moderation for the Main Post 
    # Convert the raw database row to a mutable dictionary
    post = dict(post_raw)
    # Unpack the tuple from moderate_content, we only need the moderated content string here
    moderated_post_content, _ = moderate_content(post['content'])
    post['content'] = moderated_post_content

    #  Fetch Reactions (No moderation needed) 
    reactions = query_db('''
        SELECT reaction_type, COUNT(*) as count
        FROM reactions
        WHERE post_id = ?
        GROUP BY reaction_type
    ''', (post_id,))

    #  Fetch and Moderate Comments 
    comments_raw = query_db('SELECT c.id, c.content, c.created_at, u.username, u.id as user_id FROM comments c JOIN users u ON c.user_id = u.id WHERE c.post_id = ? ORDER BY c.created_at ASC', (post_id,))
    
    comments = [] # Create a new list for the moderated comments
    for comment_raw in comments_raw:
        comment = dict(comment_raw) # Convert to a dictionary
        # Moderate the content of each comment
        print(comment['content'])
        moderated_comment_content, _ = moderate_content(comment['content'])
        comment['content'] = moderated_comment_content
        comments.append(comment)

    # Pass the moderated data to the template
    return render_template('post_detail.html.j2',
                           post=post,
                           reactions=reactions,
                           comments=comments,
                           reaction_emojis=REACTION_EMOJIS,
                           reaction_types=REACTION_TYPES)

@app.route('/about')
def about():
    return render_template('about.html.j2')

@app.route('/privacy')
def privacy():
    return render_template('privacy.html.j2')


@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        location = request.form.get('location', '')
        birthdate = request.form.get('birthdate', '')
        profile = request.form.get('profile', '')

        hashed_password = generate_password_hash(password)

        db = get_db()
        cur = db.cursor()
        try:
            cur.execute(
                'INSERT INTO users (username, password, location, birthdate, profile) VALUES (?, ?, ?, ?, ?)',
                (username, hashed_password, location, birthdate, profile)
            )
            db.commit()

            # 1. Get the ID of the user we just created.
            new_user_id = cur.lastrowid

            # 2. Add user info to the session cookie.
            session.clear() # Clear any old session data
            session['user_id'] = new_user_id
            session['username'] = username

            # 3. Flash a welcome message and redirect to the feed.
            flash(f'Welcome, {username}! Your account has been created.', 'success')
            return redirect(url_for('feed')) # Redirect to the main feed/dashboard

        except sqlite3.IntegrityError:
            flash('Username already taken. Please choose another one.', 'danger')
        finally:
            cur.close()
            db.close()
            
    return render_template('signup.html.j2')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']

        db = get_db()
        user = db.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
        db.close()

        # 1. Check if the user exists.
        # 2. If user exists, use check_password_hash to securely compare the password.
        #    This function handles the salt and prevents timing attacks.
        if user and check_password_hash(user['password'], password):
            # Password is correct!
            session['user_id'] = user['id']
            session['username'] = user['username']
            flash('Logged in successfully.', 'success')
            return redirect(url_for('feed'))
        else:
            # User does not exist or password was incorrect.
            flash('Invalid username or password.', 'danger')
            
    return render_template('login.html.j2')

@app.route('/logout')
def logout():
    session.clear()
    flash('Logged out.', 'info')
    return redirect(url_for('login'))

@app.route('/posts/<int:post_id>/comment', methods=['POST'])
def add_comment(post_id):
    """Handles adding a new comment to a specific post."""
    user_id = session.get('user_id')

    # Block access if user is not logged in
    if not user_id:
        flash('You must be logged in to comment.', 'danger')
        return redirect(url_for('login'))

    # Get content from the submitted form
    content = request.form.get('content')

    # Where to go afterwards; kept through the reminder page so we don't
    # redirect back to this POST-only URL
    back_url = local_back_url(url_for('post_detail', post_id=post_id))

    # Chapter 4, Design Claim 20: reminder before a rule-breaking comment is saved
    if content and content.strip():
        reminder = posting_reminder(content, 'comment',
                                    url_for('add_comment', post_id=post_id),
                                    {'back': back_url})
        if reminder:
            return reminder

    # Basic validation to ensure comment is not empty
    if content and content.strip():
        db = get_db()
        db.execute('INSERT INTO comments (post_id, user_id, content) VALUES (?, ?, ?)',
                   (post_id, user_id, content))
        db.commit()
        flash('Your comment was added.', 'success')
        flash_goal_feedback(user_id)
    else:
        flash('Comment cannot be empty.', 'warning')

    # Redirect back to the page the user came from (likely the post detail page)
    return redirect(back_url)

@app.route('/comments/<int:comment_id>/delete', methods=['POST'])
def delete_comment(comment_id):
    """Handles deleting a comment."""
    user_id = session.get('user_id')

    # Block access if user is not logged in
    if not user_id:
        flash('You must be logged in to delete a comment.', 'danger')
        return redirect(url_for('login'))

    # Find the comment and the original post's author ID
    comment = query_db('''
        SELECT c.id, c.user_id, p.user_id as post_author_id
        FROM comments c
        JOIN posts p ON c.post_id = p.id
        WHERE c.id = ?
    ''', (comment_id,), one=True)

    # Check if the comment exists
    if not comment:
        flash('Comment not found.', 'danger')
        return redirect(request.referrer or url_for('feed'))

    # Security Check: Allow deletion if the user is the comment's author OR the post's author
    if user_id != comment['user_id'] and user_id != comment['post_author_id']:
        flash('You do not have permission to delete this comment.', 'danger')
        return redirect(request.referrer or url_for('feed'))

    # If all checks pass, proceed with deletion
    db = get_db()
    db.execute('DELETE FROM comments WHERE id = ?', (comment_id,))
    db.commit()

    flash('Comment successfully deleted.', 'success')
    # Redirect back to the page the user came from
    return redirect(request.referrer or url_for('feed'))

@app.route('/react', methods=['POST'])
def add_reaction():
    """Handles adding a new reaction or updating an existing one."""
    user_id = session.get('user_id')

    if not user_id:
        flash("You must be logged in to react.", "danger")
        return redirect(url_for('login'))

    post_id = request.form.get('post_id')
    new_reaction_type = request.form.get('reaction')

    if not post_id or not new_reaction_type:
        flash("Invalid reaction request.", "warning")
        return redirect(request.referrer or url_for('feed'))

    db = get_db()

    # Step 1: Check if a reaction from this user already exists on this post.
    existing_reaction = query_db('SELECT id FROM reactions WHERE post_id = ? AND user_id = ?',
                                 (post_id, user_id), one=True)

    if existing_reaction:
        # Step 2: If it exists, UPDATE the reaction_type.
        db.execute('UPDATE reactions SET reaction_type = ? WHERE id = ?',
                   (new_reaction_type, existing_reaction['id']))
    else:
        # Step 3: If it does not exist, INSERT a new reaction.
        db.execute('INSERT INTO reactions (post_id, user_id, reaction_type) VALUES (?, ?, ?)',
                   (post_id, user_id, new_reaction_type))

    db.commit()

    return redirect(request.referrer or url_for('feed'))

@app.route('/unreact', methods=['POST'])
def unreact():
    """Handles removing a user's reaction from a post."""
    user_id = session.get('user_id')

    if not user_id:
        flash("You must be logged in to unreact.", "danger")
        return redirect(url_for('login'))

    post_id = request.form.get('post_id')

    if not post_id:
        flash("Invalid unreact request.", "warning")
        return redirect(request.referrer or url_for('feed'))

    db = get_db()

    # Remove the reaction if it exists
    existing_reaction = query_db(
        'SELECT id FROM reactions WHERE post_id = ? AND user_id = ?',
        (post_id, user_id),
        one=True
    )

    if existing_reaction:
        db.execute('DELETE FROM reactions WHERE id = ?', (existing_reaction['id'],))
        db.commit()
        flash("Reaction removed.", "success")
    else:
        flash("No reaction to remove.", "info")

    return redirect(request.referrer or url_for('feed'))


@app.route('/u/<int:user_id>/follow', methods=['POST'])
def follow_user(user_id):
    """Handles the logic for the current user to follow another user."""
    follower_id = session.get('user_id')

    # Security: Ensure user is logged in
    if not follower_id:
        flash("You must be logged in to follow users.", "danger")
        return redirect(url_for('login'))

    # Security: Prevent users from following themselves
    if follower_id == user_id:
        flash("You cannot follow yourself.", "warning")
        return redirect(request.referrer or url_for('feed'))

    # Check if the user to be followed actually exists
    user_to_follow = query_db('SELECT id FROM users WHERE id = ?', (user_id,), one=True)
    if not user_to_follow:
        flash("The user you are trying to follow does not exist.", "danger")
        return redirect(request.referrer or url_for('feed'))
        
    db = get_db()
    try:
        # Insert the follow relationship. The PRIMARY KEY constraint will prevent duplicates if you've set one.
        db.execute('INSERT INTO follows (follower_id, followed_id) VALUES (?, ?)',
                   (follower_id, user_id))
        db.commit()
        username_to_follow = query_db('SELECT username FROM users WHERE id = ?', (user_id,), one=True)['username']
        flash(f"You are now following {username_to_follow}.", "success")
    except sqlite3.IntegrityError:
        flash("You are already following this user.", "info")

    return redirect(request.referrer or url_for('feed'))


@app.route('/u/<int:user_id>/unfollow', methods=['POST'])
def unfollow_user(user_id):
    """Handles the logic for the current user to unfollow another user."""
    follower_id = session.get('user_id')

    # Security: Ensure user is logged in
    if not follower_id:
        flash("You must be logged in to unfollow users.", "danger")
        return redirect(url_for('login'))

    db = get_db()
    cur = db.execute('DELETE FROM follows WHERE follower_id = ? AND followed_id = ?',
               (follower_id, user_id))
    db.commit()

    if cur.rowcount > 0:
        # cur.rowcount tells us if a row was actually deleted
        username_unfollowed = query_db('SELECT username FROM users WHERE id = ?', (user_id,), one=True)['username']
        flash(f"You have unfollowed {username_unfollowed}.", "success")
    else:
        # This case handles if someone tries to unfollow a user they weren't following
        flash("You were not following this user.", "info")

    # Redirect back to the page the user came from
    return redirect(request.referrer or url_for('feed'))

@app.route('/admin')
def admin_dashboard():
    """Displays the admin dashboard with users, posts, and comments, sorted by risk."""

    if session.get('username') != 'admin':
        flash("You do not have permission to access this page.", "danger")
        return redirect(url_for('feed'))

    RISK_LEVELS = { "HIGH": 5, "MEDIUM": 3, "LOW": 1 }
    PAGE_SIZE = 50

    def get_risk_profile(score):
        if score >= RISK_LEVELS["HIGH"]:
            return "HIGH", 3
        elif score >= RISK_LEVELS["MEDIUM"]:
            return "MEDIUM", 2
        elif score >= RISK_LEVELS["LOW"]:
            return "LOW", 1
        return "NONE", 0

    # Get pagination and current tab parameters
    try:
        users_page = int(request.args.get('users_page', 1))
        posts_page = int(request.args.get('posts_page', 1))
        comments_page = int(request.args.get('comments_page', 1))
    except ValueError:
        users_page = 1
        posts_page = 1
        comments_page = 1
    
    current_tab = request.args.get('tab', 'users') # Default to 'users' tab

    users_offset = (users_page - 1) * PAGE_SIZE
    
    # First, get all users to calculate risk, then apply pagination in Python
    # It's more complex to do this efficiently in SQL if risk calc is Python-side
    all_users_raw = query_db('SELECT id, username, profile, created_at FROM users')
    all_users = []
    for user in all_users_raw:
        user_dict = dict(user)
        user_risk_score = user_risk_analysis(user_dict['id'])
        risk_label, risk_sort_key = get_risk_profile(user_risk_score)
        user_dict['risk_label'] = risk_label
        user_dict['risk_sort_key'] = risk_sort_key
        user_dict['risk_score'] = min(5.0, round(user_risk_score, 2))
        all_users.append(user_dict)

    all_users.sort(key=lambda x: x['risk_score'], reverse=True)
    total_users = len(all_users)
    users = all_users[users_offset : users_offset + PAGE_SIZE]
    total_users_pages = (total_users + PAGE_SIZE - 1) // PAGE_SIZE

    # --- Posts Tab Data ---
    posts_offset = (posts_page - 1) * PAGE_SIZE
    total_posts_count = query_db('SELECT COUNT(*) as count FROM posts', one=True)['count']
    total_posts_pages = (total_posts_count + PAGE_SIZE - 1) // PAGE_SIZE

    posts_raw = query_db(f'''
        SELECT p.id, p.content, p.created_at, u.username, u.created_at as user_created_at
        FROM posts p JOIN users u ON p.user_id = u.id
        ORDER BY p.id DESC -- Order by ID for consistent pagination before risk sort
        LIMIT ? OFFSET ?
    ''', (PAGE_SIZE, posts_offset))
    posts = []
    for post in posts_raw:
        post_dict = dict(post)
        _, base_score = moderate_content(post_dict['content'])
        final_score = base_score 
        author_created_dt = post_dict['user_created_at']
        author_age_days = (datetime.utcnow() - author_created_dt).days
        if author_age_days < 7:
            final_score *= 1.5
        risk_label, risk_sort_key = get_risk_profile(final_score)
        post_dict['risk_label'] = risk_label
        post_dict['risk_sort_key'] = risk_sort_key
        post_dict['risk_score'] = round(final_score, 2)
        posts.append(post_dict)

    posts.sort(key=lambda x: x['risk_score'], reverse=True) # Sort after fetching and scoring

    # --- Comments Tab Data ---
    comments_offset = (comments_page - 1) * PAGE_SIZE
    total_comments_count = query_db('SELECT COUNT(*) as count FROM comments', one=True)['count']
    total_comments_pages = (total_comments_count + PAGE_SIZE - 1) // PAGE_SIZE

    comments_raw = query_db(f'''
        SELECT c.id, c.content, c.created_at, u.username, u.created_at as user_created_at
        FROM comments c JOIN users u ON c.user_id = u.id
        ORDER BY c.id DESC -- Order by ID for consistent pagination before risk sort
        LIMIT ? OFFSET ?
    ''', (PAGE_SIZE, comments_offset))
    comments = []
    for comment in comments_raw:
        comment_dict = dict(comment)
        _, score = moderate_content(comment_dict['content'])
        author_created_dt = comment_dict['user_created_at']
        author_age_days = (datetime.utcnow() - author_created_dt).days
        if author_age_days < 7:
            score *= 1.5
        risk_label, risk_sort_key = get_risk_profile(score)
        comment_dict['risk_label'] = risk_label
        comment_dict['risk_sort_key'] = risk_sort_key
        comment_dict['risk_score'] = round(score, 2)
        comments.append(comment_dict)

    comments.sort(key=lambda x: x['risk_score'], reverse=True) # Sort after fetching and scoring


    return render_template('admin.html.j2', 
                           users=users, 
                           posts=posts, 
                           comments=comments,
                           
                           # Pagination for Users
                           users_page=users_page,
                           total_users_pages=total_users_pages,
                           users_has_next=(users_page < total_users_pages),
                           users_has_prev=(users_page > 1),

                           # Pagination for Posts
                           posts_page=posts_page,
                           total_posts_pages=total_posts_pages,
                           posts_has_next=(posts_page < total_posts_pages),
                           posts_has_prev=(posts_page > 1),

                           # Pagination for Comments
                           comments_page=comments_page,
                           total_comments_pages=total_comments_pages,
                           comments_has_next=(comments_page < total_comments_pages),
                           comments_has_prev=(comments_page > 1),

                           current_tab=current_tab,
                           PAGE_SIZE=PAGE_SIZE)



@app.route('/admin/delete/user/<int:user_id>', methods=['POST'])
def admin_delete_user(user_id):
    if session.get('username') != 'admin':
        flash("You do not have permission to perform this action.", "danger")
        return redirect(url_for('feed'))
        
    if user_id == session.get('user_id'):
        flash('You cannot delete your own account from the admin panel.', 'danger')
        return redirect(url_for('admin_dashboard'))
    
    db = get_db()
    db.execute('DELETE FROM users WHERE id = ?', (user_id,))
    db.commit()
    flash(f'User {user_id} and all their content has been deleted.', 'success')
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/delete/post/<int:post_id>', methods=['POST'])
def admin_delete_post(post_id):
    if session.get('username') != 'admin':
        flash("You do not have permission to perform this action.", "danger")
        return redirect(url_for('feed'))

    db = get_db()
    db.execute('DELETE FROM comments WHERE post_id = ?', (post_id,))
    db.execute('DELETE FROM reactions WHERE post_id = ?', (post_id,))
    db.execute('DELETE FROM posts WHERE id = ?', (post_id,))
    db.commit()
    flash(f'Post {post_id} has been deleted.', 'success')
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/delete/comment/<int:comment_id>', methods=['POST'])
def admin_delete_comment(comment_id):
    if session.get('username') != 'admin':
        flash("You do not have permission to perform this action.", "danger")
        return redirect(url_for('feed'))

    db = get_db()
    db.execute('DELETE FROM comments WHERE id = ?', (comment_id,))
    db.commit()
    flash(f'Comment {comment_id} has been deleted.', 'success')
    return redirect(url_for('admin_dashboard'))

@app.route('/rules')
def rules():
    return render_template('rules.html.j2')

@app.template_global()
def loop_color(user_id):
    # Generate a pastel color based on user_id hash
    h = hashlib.md5(str(user_id).encode()).hexdigest()
    r = int(h[0:2], 16)
    g = int(h[2:4], 16)
    b = int(h[4:6], 16)
    return f'rgb({r % 128 + 80}, {g % 128 + 80}, {b % 128 + 80})'


# ===================================================================
# Coding Assignment #1: Contribution and Commitment
#
# Feature 1 - Weekly contribution goals     -> Chapter 2, Design Claim 13
#             (with progress feedback, which also follows Chapter 2, Design Claim 15)
# Feature 2 - "Needs a reply" list          -> Chapter 2, Design Claim 1
# Feature 3 - Interest groups               -> Chapter 3, Design Claim 6
# ===================================================================

# ----- Feature 1: Weekly contribution goals (Chapter 2, Design Claim 13) -----
# "Providing members with specific and highly challenging goals, whether self-set
# or system-suggested, increases contribution."
# A goal is a specific number of posts and comments to write in the current week.
# The system suggests a challenging target from the user's own history, the user can
# accept or change it, and progress is fed back on the feed and after every contribution.

GOAL_LIMITS = {'posts': (1, 50), 'comments': (1, 100)}

def current_week_start():
    """Returns Monday 00:00 (UTC) of the current week. Timestamps in the DB are UTC."""
    today = datetime.utcnow().date()
    monday = today - timedelta(days=today.weekday())
    return datetime(monday.year, monday.month, monday.day)


def count_contributions(user_id, start, end):
    """Counts the posts and comments a user wrote between two datetimes."""
    start_s = start.strftime('%Y-%m-%d %H:%M:%S')
    end_s = end.strftime('%Y-%m-%d %H:%M:%S')
    posts = query_db('SELECT COUNT(*) AS cnt FROM posts WHERE user_id = ? AND created_at >= ? AND created_at < ?',
                     (user_id, start_s, end_s), one=True)['cnt']
    comments = query_db('SELECT COUNT(*) AS cnt FROM comments WHERE user_id = ? AND created_at >= ? AND created_at < ?',
                        (user_id, start_s, end_s), one=True)['cnt']
    return posts, comments


def suggest_goal(user_id):
    """
    Suggests a goal that is challenging but still realistic: one more than the user's
    best week so far, clamped to a sensible range. Very hard goals would be demotivating
    (Locke & Latham, discussed in Chapter 2 next to Design Claim 13).
    """
    def best_week(table):
        row = query_db(f"""
            SELECT MAX(cnt) AS best FROM (
                SELECT COUNT(*) AS cnt FROM {table} WHERE user_id = ?
                GROUP BY strftime('%Y-%W', created_at)
            )""", (user_id,), one=True)
        return row['best'] or 0

    posts_target = min(max(best_week('posts') + 1, 2), 10)
    comments_target = min(max(best_week('comments') + 1, 5), 30)
    return {'posts_target': posts_target, 'comments_target': comments_target}


def get_goal_progress(user_id):
    """Returns the user's goal and this week's progress, or None if no goal is set."""
    goal = query_db('SELECT posts_target, comments_target FROM contribution_goals WHERE user_id = ?',
                    (user_id,), one=True)
    if not goal:
        return None

    week_start = current_week_start()
    posts_done, comments_done = count_contributions(user_id, week_start, week_start + timedelta(days=7))

    # Overall progress only counts contributions up to each target,
    # so extra posts cannot hide missing comments.
    total_target = goal['posts_target'] + goal['comments_target']
    total_done = min(posts_done, goal['posts_target']) + min(comments_done, goal['comments_target'])

    return {
        'posts_target': goal['posts_target'],
        'comments_target': goal['comments_target'],
        'posts_done': posts_done,
        'comments_done': comments_done,
        'percent': int(100 * total_done / total_target),
        'remaining': total_target - total_done,
        'completed': total_done >= total_target,
        'days_left': 7 - datetime.utcnow().weekday(),  # includes today
    }


def flash_goal_feedback(user_id):
    """
    Chapter 2, Design Claim 15: goals work better with frequent feedback.
    Called right after a post or comment so the user immediately sees their progress.
    """
    progress = get_goal_progress(user_id)
    if not progress:
        return
    if progress['completed']:
        flash('Weekly goal reached! You met your goal of '
              f"{progress['posts_target']} posts and {progress['comments_target']} comments.", 'success')
    else:
        flash(f"Weekly goal: {progress['posts_done']}/{progress['posts_target']} posts, "
              f"{progress['comments_done']}/{progress['comments_target']} comments "
              f"({progress['percent']}%). {progress['remaining']} to go!", 'info')


@app.route('/goals', methods=['GET', 'POST'])
def goals():
    """Shows the user's weekly goal and history, and lets them set or change the goal."""
    user_id = session.get('user_id')
    if not user_id:
        flash('You must be logged in to set a goal.', 'danger')
        return redirect(url_for('login'))

    if request.method == 'POST':
        posts_target = request.form.get('posts_target', type=int)
        comments_target = request.form.get('comments_target', type=int)

        # Validate that both targets are whole numbers within the allowed range
        p_min, p_max = GOAL_LIMITS['posts']
        c_min, c_max = GOAL_LIMITS['comments']
        if posts_target is None or comments_target is None \
                or not p_min <= posts_target <= p_max or not c_min <= comments_target <= c_max:
            flash(f'Goals must be {p_min}-{p_max} posts and {c_min}-{c_max} comments.', 'warning')
            return redirect(url_for('goals'))

        # One goal per user: replace any previous goal
        query_db('INSERT OR REPLACE INTO contribution_goals (user_id, posts_target, comments_target) VALUES (?, ?, ?)',
                 (user_id, posts_target, comments_target), commit=True)
        flash(f'Goal set: {posts_target} posts and {comments_target} comments this week.', 'success')
        return redirect(request.form.get('next') or url_for('goals'))

    progress = get_goal_progress(user_id)

    # History of the last 6 weeks, compared against the current goal
    history = []
    if progress:
        week_start = current_week_start()
        for weeks_ago in range(1, 7):
            start = week_start - timedelta(weeks=weeks_ago)
            posts_done, comments_done = count_contributions(user_id, start, start + timedelta(days=7))
            history.append({
                'week_start': start,
                'posts_done': posts_done,
                'comments_done': comments_done,
                'met': posts_done >= progress['posts_target'] and comments_done >= progress['comments_target'],
            })

    return render_template('goals.html.j2',
                           progress=progress,
                           suggested_goal=suggest_goal(user_id),
                           history=history,
                           limits=GOAL_LIMITS)


@app.route('/goals/delete', methods=['POST'])
def delete_goal():
    """Removes the user's weekly goal."""
    user_id = session.get('user_id')
    if not user_id:
        return redirect(url_for('login'))
    query_db('DELETE FROM contribution_goals WHERE user_id = ?', (user_id,), commit=True)
    flash('Your weekly goal was removed.', 'info')
    return redirect(url_for('goals'))


# ----- Feature 2: "Needs a reply" list (Chapter 2, Design Claim 1) -----
# "Making the list of needed contributions easily visible increases the likelihood
# that the community will provide them."
# A needed contribution is defined as a reply to a post by someone you follow that has
# received few or no comments, and that you have not replied to yet. Posts with the
# fewest replies are listed first, and the count is always visible in the navigation bar.

NEEDS_REPLY_MAX_COMMENTS = 2   # posts with fewer comments than this "need a reply"
NEEDS_REPLY_PAGE_SIZE = 20

NEEDS_REPLY_WHERE = """
    WHERE p.user_id IN (SELECT followed_id FROM follows WHERE follower_id = :uid)
      AND NOT EXISTS (SELECT 1 FROM comments c WHERE c.post_id = p.id AND c.user_id = :uid)
      AND (SELECT COUNT(*) FROM comments c WHERE c.post_id = p.id) < :max_comments
"""

def count_needs_reply(user_id):
    """Number of posts currently waiting for this user's reply (shown in the navbar)."""
    row = query_db(f'SELECT COUNT(*) AS cnt FROM posts p {NEEDS_REPLY_WHERE}',
                   {'uid': user_id, 'max_comments': NEEDS_REPLY_MAX_COMMENTS}, one=True)
    return row['cnt'] if row else 0


@app.context_processor
def inject_needs_reply_count():
    """Makes the needs-reply count available in every template (used by the navbar badge)."""
    user_id = session.get('user_id')
    return {'needs_reply_count': count_needs_reply(user_id) if user_id else 0}


@app.route('/needs-reply')
def needs_reply():
    """Lists followed users' posts that still need a reply, fewest replies first."""
    user_id = session.get('user_id')
    if not user_id:
        flash('You must be logged in to see posts that need a reply.', 'danger')
        return redirect(url_for('login'))

    posts_raw = query_db(f"""
        SELECT p.id, p.content, p.created_at, u.username, u.id AS user_id,
               (SELECT COUNT(*) FROM comments c WHERE c.post_id = p.id) AS comment_count
        FROM posts p JOIN users u ON p.user_id = u.id
        {NEEDS_REPLY_WHERE}
        ORDER BY comment_count ASC, p.created_at DESC
        LIMIT :limit
    """, {'uid': user_id, 'max_comments': NEEDS_REPLY_MAX_COMMENTS, 'limit': NEEDS_REPLY_PAGE_SIZE})

    posts = []
    for post_raw in posts_raw:
        post = dict(post_raw)
        post['content'], _ = moderate_content(post['content'])
        posts.append(post)

    following_count = query_db('SELECT COUNT(*) AS cnt FROM follows WHERE follower_id = ?',
                               (user_id,), one=True)['cnt']

    return render_template('needs_reply.html.j2',
                           posts=posts,
                           total=count_needs_reply(user_id),
                           following_count=following_count,
                           goal_progress=get_goal_progress(user_id))


# ----- Feature 3: Interest groups (Chapter 3, Design Claim 6) -----
# "Creating named groups within a larger online community increases members'
# commitment to the subgroups."
# Users can create a named group with a description, join or leave groups, and post
# inside a group. Group posts are labelled with the group name on the main feed,
# and a user's groups are listed on their profile.

def is_group_member(group_id, user_id):
    return query_db('SELECT 1 FROM group_members WHERE group_id = ? AND user_id = ?',
                    (group_id, user_id), one=True) is not None


@app.route('/groups')
def groups_index():
    """Lists all groups with member and post counts. Groups the user joined are listed first."""
    user_id = session.get('user_id')
    groups = query_db('''
        SELECT ig.id, ig.name, ig.description, ig.created_at,
               (SELECT COUNT(*) FROM group_members gm WHERE gm.group_id = ig.id) AS member_count,
               (SELECT COUNT(*) FROM posts p WHERE p.group_id = ig.id) AS post_count,
               EXISTS (SELECT 1 FROM group_members gm WHERE gm.group_id = ig.id AND gm.user_id = ?) AS is_member
        FROM interest_groups ig
        ORDER BY is_member DESC, member_count DESC, ig.name
    ''', (user_id or 0,))
    # Chapter 5, Design Claim 25: newcomers see that creating groups is still locked
    newcomer = newcomer_status(user_id) if user_id else None
    return render_template('groups.html.j2', groups=groups, newcomer=newcomer)


@app.route('/groups/new', methods=['POST'])
def create_group():
    """Creates a new named group. The creator automatically becomes its first member."""
    user_id = session.get('user_id')
    if not user_id:
        flash('You must be logged in to create a group.', 'danger')
        return redirect(url_for('login'))

    # Chapter 5, Design Claim 25: creating groups is unlocked after the newcomer period
    if newcomer_status(user_id)['is_newcomer']:
        flash('New members can join groups, but creating one unlocks after '
              f'{NEWCOMER_UNLOCK_CONTRIBUTIONS} posts or comments.', 'warning')
        return redirect(url_for('groups_index'))

    name = (request.form.get('name') or '').strip()
    description = (request.form.get('description') or '').strip()

    if not 3 <= len(name) <= 40:
        flash('Group name must be 3-40 characters long.', 'warning')
        return redirect(url_for('groups_index'))
    if query_db('SELECT 1 FROM interest_groups WHERE name = ?', (name,), one=True):
        flash('A group with that name already exists. Join it instead!', 'warning')
        return redirect(url_for('groups_index'))

    cur = query_db('INSERT INTO interest_groups (name, description, creator_id) VALUES (?, ?, ?)',
                   (name, description[:300], user_id), commit=True)
    group_id = cur.lastrowid
    query_db('INSERT INTO group_members (group_id, user_id) VALUES (?, ?)', (group_id, user_id), commit=True)

    flash(f'Group "{name}" created. Invite others by sharing its page!', 'success')
    return redirect(url_for('group_detail', group_id=group_id))


@app.route('/groups/<int:group_id>')
def group_detail(group_id):
    """Group page: name, description, members, join/leave button and the group's own feed."""
    group = query_db('''
        SELECT ig.*, u.username AS creator_name FROM interest_groups ig
        JOIN users u ON ig.creator_id = u.id WHERE ig.id = ?
    ''', (group_id,), one=True)
    if not group:
        abort(404)

    user_id = session.get('user_id')
    members = query_db('''
        SELECT u.id, u.username FROM group_members gm JOIN users u ON gm.user_id = u.id
        WHERE gm.group_id = ? ORDER BY gm.joined_at
    ''', (group_id,))

    posts_raw = query_db('''
        SELECT p.id, p.content, p.created_at, u.username, u.id AS user_id,
               (SELECT COUNT(*) FROM comments c WHERE c.post_id = p.id) AS comment_count
        FROM posts p JOIN users u ON p.user_id = u.id
        WHERE p.group_id = ? ORDER BY p.created_at DESC LIMIT 50
    ''', (group_id,))
    posts = []
    for post_raw in posts_raw:
        post = dict(post_raw)
        post['content'], _ = moderate_content(post['content'])
        posts.append(post)

    return render_template('group_detail.html.j2',
                           group=group,
                           members=members,
                           posts=posts,
                           is_member=bool(user_id) and is_group_member(group_id, user_id))


@app.route('/groups/<int:group_id>/join', methods=['POST'])
def join_group(group_id):
    user_id = session.get('user_id')
    if not user_id:
        flash('You must be logged in to join a group.', 'danger')
        return redirect(url_for('login'))
    group = query_db('SELECT name FROM interest_groups WHERE id = ?', (group_id,), one=True)
    if not group:
        abort(404)
    # INSERT OR IGNORE: joining twice has no effect
    query_db('INSERT OR IGNORE INTO group_members (group_id, user_id) VALUES (?, ?)',
             (group_id, user_id), commit=True)
    flash(f'Welcome to {group["name"]}!', 'success')
    return redirect(url_for('group_detail', group_id=group_id))


@app.route('/groups/<int:group_id>/leave', methods=['POST'])
def leave_group(group_id):
    user_id = session.get('user_id')
    if not user_id:
        return redirect(url_for('login'))
    query_db('DELETE FROM group_members WHERE group_id = ? AND user_id = ?',
             (group_id, user_id), commit=True)
    flash('You left the group.', 'info')
    return redirect(url_for('group_detail', group_id=group_id))


# =====================================================================
# Coding Assignment #2: Posting reminder
# Chapter 4, Design Claim 20: "Reminders of the rules at the point where
# people might violate them lead to fewer violations."
#
# Before a post or comment is saved, it is checked with the moderation rules.
# If it would be censored, removed or flagged, it is not saved right away.
# Instead they see which rule it breaks and a preview of how others will see it,
# and can edit the text or choose to post anyway.
# =====================================================================

def local_back_url(fallback):
    """Returns the page the user came from, but only if it is on this site."""
    back = request.form.get('back') or request.referrer or ''
    parsed = urlparse(back)
    if parsed.netloc and parsed.netloc != request.host:
        return fallback
    path = parsed.path or ''
    if not path.startswith('/'):
        return fallback
    return path + ('?' + parsed.query if parsed.query else '')


def posting_reminder(content, kind, form_action, hidden_fields):
    """
    Shows the reminder page if `content` breaks a rule and the user has not
    already confirmed. Returns a rendered page, or None if posting can go ahead.

    kind:          'post' or 'comment' (used in the wording)
    form_action:   URL the edited text is sent back to
    hidden_fields: extra form values to keep (group_id, back URL)
    """
    if request.form.get('confirmed') == '1':
        return None

    moderated, score, reasons = check_content(content)
    if score == 0:
        return None

    # Rule 2.1: content from accounts under 7 days old counts 1.5x
    user = query_db('SELECT created_at FROM users WHERE id = ?', (session['user_id'],), one=True)
    is_new_account = (datetime.utcnow() - user['created_at']).days < 7
    if is_new_account:
        score *= 1.5

    return render_template('post_reminder.html.j2',
                           kind=kind,
                           content=content,
                           moderated=moderated,
                           reasons=reasons,
                           score=round(min(score, 5.0), 2),
                           removed=moderated.startswith('[content removed'),
                           is_new_account=is_new_account,
                           form_action=form_action,
                           hidden_fields=hidden_fields)


# =====================================================================
# Coding Assignment #3: Dealing with newcomers
# =====================================================================

# ---------------------------------------------------------------------
# Feature 1 - Chapter 5, Design Claim 19:
# "Public profiles and introduction threads increase interaction
#  between old-timers and newcomers."
# Newcomers are asked to write an introduction post. All introductions
# are collected on the /newcomers page where members can welcome them,
# and the intro is pinned on the newcomer's profile.
# ---------------------------------------------------------------------

def get_intro_post(user_id):
    """Returns the user's introduction post, or None if they have not written one."""
    return query_db('SELECT id, content, created_at FROM posts WHERE user_id = ? AND is_intro = 1 '
                    'ORDER BY id LIMIT 1', (user_id,), one=True)


# Introductions from the last 30 days that the viewer has not commented on yet
UNWELCOMED_INTROS_WHERE = """
    p.is_intro = 1
    AND p.user_id != :uid
    AND p.created_at >= datetime('now', '-30 days')
    AND NOT EXISTS (SELECT 1 FROM comments c WHERE c.post_id = p.id AND c.user_id = :uid)
"""


@app.context_processor
def inject_unwelcomed_count():
    """Number shown next to 'New members' in the navbar."""
    user_id = session.get('user_id')
    if not user_id:
        return {'unwelcomed_count': 0}
    row = query_db(f'SELECT COUNT(*) AS n FROM posts p WHERE {UNWELCOMED_INTROS_WHERE}',
                   {'uid': user_id}, one=True)
    return {'unwelcomed_count': row['n']}


@app.route('/newcomers')
def newcomers():
    """Lists introduction posts so members can welcome newcomers."""
    user_id = session.get('user_id') or 0

    # Intros the viewer has not welcomed yet come first, then newest first
    intros = query_db('''
        SELECT p.id, p.content, p.created_at, u.username, u.created_at AS joined_at,
               (SELECT COUNT(*) FROM comments c WHERE c.post_id = p.id AND c.user_id != p.user_id) AS welcome_count,
               EXISTS (SELECT 1 FROM comments c WHERE c.post_id = p.id AND c.user_id = :uid) AS welcomed_by_me
        FROM posts p JOIN users u ON u.id = p.user_id
        WHERE p.is_intro = 1
        ORDER BY welcomed_by_me ASC, p.created_at DESC
        LIMIT 50
    ''', {'uid': user_id})
    intros = [dict(row) for row in intros]
    for intro in intros:
        intro['content'], _ = moderate_content(intro['content'])

    # Recently joined members who have not introduced themselves yet (public profiles)
    quiet_members = query_db('''
        SELECT u.username, u.created_at FROM users u
        WHERE u.created_at >= datetime('now', '-30 days')
          AND NOT EXISTS (SELECT 1 FROM posts p WHERE p.user_id = u.id AND p.is_intro = 1)
        ORDER BY u.created_at DESC LIMIT 20
    ''')

    newcomer = newcomer_status(user_id) if user_id else None
    return render_template('newcomers.html.j2', intros=intros,
                           quiet_members=quiet_members, newcomer=newcomer)


# ---------------------------------------------------------------------
# Feature 2 - Chapter 5, Design Claim 25:
# "Progressive access controls reduce the harm newcomers can do."
# A new account starts with limited rights: at most 3 posts per day and
# no creating groups. Full access unlocks after 5 posts/comments that pass
# moderation, or automatically after 7 days. The user sees their progress.
# ---------------------------------------------------------------------
NEWCOMER_DAYS = 7
NEWCOMER_UNLOCK_CONTRIBUTIONS = 5
NEWCOMER_DAILY_POSTS = 3


def count_clean_contributions(user_id):
    """
    Posts + comments that pass moderation (score below 1.0).
    Censored or removed content does not count, so spamming bad posts
    does not unlock access faster.
    """
    rows = query_db('SELECT content FROM posts WHERE user_id = ? '
                    'UNION ALL SELECT content FROM comments WHERE user_id = ?', (user_id, user_id))
    return sum(1 for row in rows if moderate_content(row['content'])[1] < 1.0)


def newcomer_status(user_id):
    """Returns a dict describing the user's newcomer level and what is still locked."""
    user = query_db('SELECT created_at FROM users WHERE id = ?', (user_id,), one=True)
    if not user:
        return {'is_newcomer': False, 'posts_today': 0, 'has_intro': True}

    age_days = (datetime.utcnow() - user['created_at']).days
    contributions = count_clean_contributions(user_id)
    is_newcomer = age_days < NEWCOMER_DAYS and contributions < NEWCOMER_UNLOCK_CONTRIBUTIONS

    today_start = datetime.utcnow().strftime('%Y-%m-%d 00:00:00')
    posts_today = query_db('SELECT COUNT(*) AS n FROM posts WHERE user_id = ? AND created_at >= ?',
                           (user_id, today_start), one=True)['n']

    return {
        'is_newcomer': is_newcomer,
        'contributions': min(contributions, NEWCOMER_UNLOCK_CONTRIBUTIONS),
        'needed': NEWCOMER_UNLOCK_CONTRIBUTIONS,
        'percent': int(100 * min(contributions, NEWCOMER_UNLOCK_CONTRIBUTIONS) / NEWCOMER_UNLOCK_CONTRIBUTIONS),
        'days_left': max(0, NEWCOMER_DAYS - age_days),
        'posts_today': posts_today,
        'posts_left': max(0, NEWCOMER_DAILY_POSTS - posts_today),
        'daily_limit': NEWCOMER_DAILY_POSTS,
        'has_intro': get_intro_post(user_id) is not None,
    }


# ---------------------------------------------------------------------
# Helpers for the recommendation algorithm (Assignment 3.1)
# ---------------------------------------------------------------------
POSITIVE_REACTIONS = ('like', 'love', 'haha', 'wow')

STOP_WORDS = set('''
a an the and or but if so of to in on at for with from by about as into is are was were be been
being am i me my we our you your he she it its they them their this that these those there here
just really very so too not no yes do does did done have has had will would can could should
what when where who how all any some more most much many one two get got make made out up down
over again then than now new day today time like love feel feeling great good best amazing
finally ever even still also only s t ve ll re m d'''.split())


def tokenize(text):
    """
    Splits text into lowercase keywords and drops common words.
    Hashtags are split on capitals: #DIYLife -> diy, life
    """
    tokens = []
    for word in re.findall(r'#?\w+', text or ''):
        if word.startswith('#'):
            parts = re.findall(r'[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+', word[1:])
        else:
            parts = [word]
        for part in parts:
            part = part.lower()
            if len(part) > 2 and part not in STOP_WORDS and not part.isdigit():
                tokens.append(part)
    return tokens


# ----- Functions to be implemented are below
# Coding Assignment #2

# Assignment 2.2
def user_risk_analysis(user_id):
    """
    Args:
        user_id: The ID of the user on which we perform risk analysis.

    Returns:
        A float number score showing the risk associated with this user. There are no strict rules or bounds to this score, other than that a score of less than 1.0 means no risk, 1.0 to 3.0 is low risk, 3.0 to 5.0 is medium risk and above 5.0 is high risk. (An upper bound of 5.0 is applied to this score elsewhere in the codebase) 
        
        You will be able to check the scores by logging in with the administrator account:
            username: admin
            password: admin
        Then, navigate to the /admin endpoint. (http://localhost:8080/admin)
    """

    # Rule 2.2 from the rules page:
    # content_risk = profile*1 + average post score*3 + average comment score*1,
    # then multiplied by an account-age factor and capped at 5.0.
    user = query_db('SELECT profile, created_at FROM users WHERE id = ?', (user_id,), one=True)
    if not user:
        return 0.0

    profile_score = moderate_content(user['profile'] or '')[1]

    # Averages use the base content score (no per-post age multiplier); 0 if there is nothing.
    post_rows = query_db('SELECT content FROM posts WHERE user_id = ?', (user_id,))
    comment_rows = query_db('SELECT content FROM comments WHERE user_id = ?', (user_id,))
    post_scores = [moderate_content(row['content'])[1] for row in post_rows]
    comment_scores = [moderate_content(row['content'])[1] for row in comment_rows]
    average_post_score = sum(post_scores) / len(post_scores) if post_scores else 0.0
    average_comment_score = sum(comment_scores) / len(comment_scores) if comment_scores else 0.0

    content_risk_score = profile_score * 1 + average_post_score * 3 + average_comment_score * 1

    # Newer accounts are treated as riskier
    age_days = (datetime.utcnow() - user['created_at']).days
    if age_days < 7:
        content_risk_score *= 1.5
    elif age_days < 30:
        content_risk_score *= 1.2

    return min(content_risk_score, 5.0)


# Regexes for the word lists are built once at start-up instead of on every call.
# (?<!\w) and (?!\w) are "whole word" boundaries that also work when a phrase
# starts or ends with a symbol (some tier 2 phrases do).
def _whole_word_pattern(words):
    alternatives = sorted((re.escape(w) for w in words), key=len, reverse=True)
    return re.compile(r'(?<!\w)(?:' + '|'.join(alternatives) + r')(?!\w)', re.IGNORECASE)

TIER1_PATTERN = _whole_word_pattern(TIER1_WORDS)
TIER2_PATTERN = _whole_word_pattern(TIER2_PHRASES)
TIER3_PATTERN = _whole_word_pattern(TIER3_WORDS)
# A link is anything starting with http(s):// or www.
LINK_PATTERN = re.compile(r'(?:https?://|www\.)\S+', re.IGNORECASE)


def check_content(content):
    """
    Runs the moderation rules (Part 1 of the rules page) on a piece of text.

    Returns (moderated_content, score, reasons). `reasons` is a list of short
    human-readable explanations; the posting reminder (Chapter 4, Design Claim 20)
    shows them to the user before they publish.
    """
    content = content or ''

    # Stage 1.1: severe violations remove the whole text and stop processing.
    if TIER1_PATTERN.search(content):
        return ('[content removed due to severe violation]', 5.0,
                ['It contains a severe word, so the whole text will be removed.'])
    if TIER2_PATTERN.search(content):
        return ('[content removed due to spam/scam policy]', 5.0,
                ['It contains a spam or scam phrase, so the whole text will be removed.'])

    # Stage 1.2: scored violations, starting from 0.0
    score = 0.0
    reasons = []

    # Tier 3: mask each mild word with the same number of asterisks, +2.0 each
    moderated, tier3_count = TIER3_PATTERN.subn(lambda m: '*' * len(m.group(0)), content)
    if tier3_count:
        score += 2.0 * tier3_count
        reasons.append(f'{tier3_count} mild swear word(s) will be hidden with *****.')

    # External links: replace each one, +2.0 each
    moderated, link_count = LINK_PATTERN.subn('[link removed]', moderated)
    if link_count:
        score += 2.0 * link_count
        reasons.append(f'{link_count} link(s) will be replaced with [link removed].')

    # Excessive capitals: more than 15 letters and over 70% uppercase, +0.5 once.
    # Counted on the original text; the text itself is not changed.
    letters = [ch for ch in content if ch.isalpha()]
    if len(letters) > 15 and sum(ch.isupper() for ch in letters) / len(letters) > 0.7:
        score += 0.5
        reasons.append('It is mostly in CAPITAL letters, which reads like shouting.')

    return moderated, score, reasons


# Assignment 2.1
def moderate_content(content):
    """
    Args
        content: the text content of a post or comment to be moderated.
        
    Returns: 
        A tuple containing the moderated content (string) and a severity score (float). There are no strict rules or bounds to the severity score, other than that a score of less than 1.0 means no risk, 1.0 to 3.0 is low risk, 3.0 to 5.0 is medium risk and above 5.0 is high risk.
    
    This function moderates a string of content and calculates a severity score based on
    rules loaded from the 'censorship.dat' file. These are already loaded as TIER1_WORDS, TIER2_PHRASES and TIER3_WORDS. Tier 1 corresponds to strong profanity, Tier 2 to scam/spam phrases and Tier 3 to mild profanity.
    
    You will be able to check the scores by logging in with the administrator account:
            username: admin
            password: admin
    Then, navigate to the /admin endpoint. (http://localhost:8080/admin)
    """

    # The rules live in check_content(); here we only drop the list of reasons.
    moderated_content, score, _ = check_content(content)
    return moderated_content, score

# Coding Assignment #3
# Assignment 3.1
def recommend(user_id, filter_following):
    """
    Args:
        user_id: The ID of the current user.
        filter_following: Boolean, True if we only want to see recommendations from followed users.

    Returns:
        A list of 5 recommended posts, in reverse-chronological order.

    To test whether your recommendation algorithm works, let's pretend we like the DIY topic. Here are some users that often post DIY comment and a few example posts. Make sure your account did not engage with anything else. You should test your algorithm with these and see if your recommendation algorithm picks up on your interest in DIY and starts showing related content.
    
    Users: @starboy99, @DancingDolphin, @blogger_bob
    Posts: 1810, 1875, 1880, 2113
    
    Materials: 
    - https://www.nvidia.com/en-us/glossary/recommendation-system/
    - http://www.configworks.com/mz/handout_recsys_sac2010.pdf
    - https://www.researchgate.net/publication/227268858_Recommender_Systems_Handbook
    """

    # Hybrid recommender:
    #  1. Content-based: build a keyword profile (TF-IDF) from the posts the user
    #     reacted to positively, and score other posts by cosine similarity to it.
    #  2. Social: small bonus for authors the user liked before or follows.
    #  3. Cold start: if there is no signal yet, fall back to the most reacted posts.
    all_posts = query_db('''
        SELECT p.id, p.content, p.created_at, u.username, u.id AS user_id,
               ig.id AS group_id, ig.name AS group_name, p.is_intro,
               IFNULL(r.total_reactions, 0) AS total_reactions
        FROM posts p
        JOIN users u ON p.user_id = u.id
        LEFT JOIN interest_groups ig ON p.group_id = ig.id
        LEFT JOIN (SELECT post_id, COUNT(*) AS total_reactions FROM reactions GROUP BY post_id) r
               ON r.post_id = p.id
    ''')
    posts_by_id = {post['id']: dict(post) for post in all_posts}

    # IDF: words that appear in many posts ("today", "fun") get a low weight,
    # rare topic words ("diy", "terrarium") get a high weight.
    post_tokens = {pid: collections.Counter(tokenize(post['content'])) for pid, post in posts_by_id.items()}
    doc_freq = collections.Counter()
    for tokens in post_tokens.values():
        doc_freq.update(tokens.keys())
    total_docs = len(posts_by_id) or 1
    idf = {term: math.log(total_docs / (1 + df)) + 1 for term, df in doc_freq.items()}

    def tfidf_vector(tokens):
        vec = {term: count * idf.get(term, 1.0) for term, count in tokens.items()}
        norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
        return {term: v / norm for term, v in vec.items()}

    liked_ids, reacted_ids, followed = set(), set(), set()
    if user_id:
        for row in query_db('SELECT post_id, reaction_type FROM reactions WHERE user_id = ?', (user_id,)):
            reacted_ids.add(row['post_id'])
            if row['reaction_type'] in POSITIVE_REACTIONS:
                liked_ids.add(row['post_id'])
        followed = {row['followed_id'] for row in
                    query_db('SELECT followed_id FROM follows WHERE follower_id = ?', (user_id,))}

    # The user's interest profile = sum of the TF-IDF vectors of liked posts
    profile = collections.Counter()
    liked_authors = collections.Counter()
    for pid in liked_ids:
        if pid in posts_by_id:
            profile.update(tfidf_vector(post_tokens[pid]))
            liked_authors[posts_by_id[pid]['user_id']] += 1
    profile_norm = math.sqrt(sum(v * v for v in profile.values())) or 1.0

    # Candidates: not the user's own posts and not posts they already reacted to
    candidates = [post for pid, post in posts_by_id.items()
                  if post['user_id'] != user_id and pid not in reacted_ids
                  and (not filter_following or post['user_id'] in followed)]

    max_reactions = max((post['total_reactions'] for post in candidates), default=0) or 1
    for post in candidates:
        if profile:
            vec = tfidf_vector(post_tokens[post['id']])
            content_score = sum(weight * profile.get(term, 0) for term, weight in vec.items()) / profile_norm
        else:
            content_score = 0.0
        author_score = 0.2 * min(liked_authors[post['user_id']], 3) / 3 + (0.1 if post['user_id'] in followed else 0)
        popularity_score = 0.05 * post['total_reactions'] / max_reactions
        post['score'] = content_score + author_score + popularity_score

    # Pick the 5 best, then show them newest first as the docstring asks
    top = sorted(candidates, key=lambda post: post['score'], reverse=True)[:5]
    recommended_posts = sorted(top, key=lambda post: post['created_at'], reverse=True)

    return recommended_posts

if __name__ == '__main__':
    app.run(debug=True, port=8080)

