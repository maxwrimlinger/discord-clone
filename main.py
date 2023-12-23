import os
import json
import requests
from flask import Flask, render_template, request, redirect
import utils

# Google authentication
from google.cloud import datastore
from oauthlib.oauth2 import WebApplicationClient
from flask_login import LoginManager, current_user, login_required, login_user, logout_user

# handling datetime sent for messages
from datetime import datetime, timezone

app = Flask(__name__)
app.secret_key = os.urandom(12).hex() # needed for SSL
db = datastore.Client()

login_manager = LoginManager()
login_manager.init_app(app)

@login_manager.user_loader
def load_user(user_id):
    db_user = db.get(db.key("user", user_id))
    user = User(
        user_id,
        db_user["first_name"],
        db_user["last_name"],
        db_user["email"],
        db_user["picture"],
        db_user["last_login"],
    )
    return user

# globals for various google auth references
client_secrets = utils.get_client_secrets()
auth_client = WebApplicationClient(client_secrets["client_id"])
GOOGLE_DISCOVERY_URL = "https://accounts.google.com/.well-known/openid-configuration"
USERINFO_ENDPOINT = requests.get(GOOGLE_DISCOVERY_URL).json()["userinfo_endpoint"]

class User():
    """
    For use with Flask-Login which holds on to an object as the `current_user`.
    """
    def __init__(self, id, first_name, last_name, email, profile_picture, last_login):
        self.id = id
        self.first_name = first_name
        self.last_name = last_name
        self.email = email
        self.profile_picture = profile_picture
        self.last_login = last_login

        # required for flask_login
        self.is_anonymous = False
        self.is_authenticated = True
        self.is_active = True

    def get_id(self):
        return str(self.id)

class Message():
    """
    Represents a chat message. This class does the heavy lifting of
    converting the date and time the message was sent to a more readible format
    for the front end.
    """
    def __init__(self, channel, content, datetime_sent, message_id, author_first_name, author_last_name, author_profile_picture):
        self.channel = channel
        self.content = content
        self.datetime_sent = utils.get_relational_datetime(datetime_sent)
        self.id = message_id
        self.author_first_name = author_first_name
        self.author_last_name = author_last_name
        self.author_profile_picture = author_profile_picture

def channel_query():
    query = db.query(kind="channel")
    query.order = ["name"]
    return list(query.fetch())

# Routes

@app.route("/")
def index():
    if current_user.is_authenticated:
        return redirect("/channel")
    else:
        return render_template("/login.html")

@app.route("/login")
def login():
    request_uri = auth_client.prepare_request_uri(
        client_secrets["auth_uri"],
        redirect_uri="https://127.0.0.1:8080/callback",
        scope=["openid", "email", "profile"],
    )
    return redirect(request_uri)

@app.route("/logout")
def logout():
    logout_user()
    return redirect("/")


@app.route("/callback")
def callback():
    code = request.args.get("code")
    token_url, headers, body = auth_client.prepare_token_request(
        client_secrets["token_uri"],
        authorization_response=request.url,
        redirect_url=request.base_url,
        code=code,
    )
    token_response = requests.post(
        token_url,
        headers=headers,
        data=body,
        auth=(client_secrets["client_id"], client_secrets["client_secret"]),
    )
    auth_client.parse_request_body_response(json.dumps(token_response.json()))
    
    uri, headers, body = auth_client.add_token(USERINFO_ENDPOINT)
    userinfo_response = requests.get(uri, headers=headers, data=body).json()

    if not userinfo_response.get("email_verified"):
        return "User email not available or not verified by Google.", 400

    now = datetime.now(timezone.utc)

    print(userinfo_response["sub"])

    user = User(
        userinfo_response["sub"], userinfo_response["given_name"], 
        userinfo_response["family_name"], userinfo_response["email"], 
        userinfo_response["picture"], now
    )

    user_entity = datastore.Entity(db.key("user", user.id))
    user_entity.update(
        {
            "first_name": user.first_name,
            "last_name": user.last_name,
            "email": user.email,
            "picture": user.profile_picture,
            "last_login": now
        }
    )
    db.put(user_entity)

    login_user(user)

    return redirect("/channel")

@app.route("/channel/")
@login_required
def channel_index():
    first_channel = channel_query()[0]
    return redirect("/channel/" + first_channel["name"])

@app.route("/channel/<selected_channel_name>/", methods=["POST", "GET"])
@login_required
def channel(selected_channel_name):
    if request.method == "POST":
        message_content = request.form["content"]
        if message_content.isspace() or message_content == "": 
            return redirect("/channel/" + selected_channel_name)
        
        message = datastore.Entity(db.key("message"))
        message.update(
            {
                "channel": selected_channel_name,
                "content": message_content,
                "datetime_sent": datetime.now(timezone.utc),
                "author": current_user.id
            }
        )
        db.put(message)
    
        return redirect("/channel/" + selected_channel_name)
    else:
        channels = channel_query()
        channel_names = [channel["name"] for channel in channels]
        if selected_channel_name not in channel_names:
            return "Channel not found"

        message_query = db.query(kind="message")
        message_query.add_filter("channel", "=", selected_channel_name)
        message_query.order = ["datetime_sent"]
        messages = list(message_query.fetch())
        formatted_messages = []
        for message in messages:
            author = db.get(db.key("user", message["author"]))
            formatted_message = Message(message["channel"],
                                        message["content"], 
                                        message["datetime_sent"], 
                                        message.id,
                                        author["first_name"],
                                        author["last_name"],
                                        author["picture"])
            formatted_messages.append(formatted_message)
        return render_template(
            "index.html", 
            selected_channel_name=selected_channel_name, channels=channels, 
            messages=formatted_messages, user_first_name=current_user.first_name,
            user_last_name=current_user.last_name, user_profile_picture=current_user.profile_picture
        )

@app.route("/add-channel", methods=["POST", "GET"])
@login_required
def add_channel():
    if request.method == "POST":
        channel_name = request.form["channel-name"]

        # don't recreate already existing channels
        channels = channel_query()
        channel_names = [channel["name"] for channel in channels]
        if channel_name in channel_names:
            return redirect("/channel/" + channel_name)

        channel = datastore.Entity(db.key("channel"))
        channel.update(
            {
                "name": channel_name,
                "datetime_created": datetime.now(timezone.utc),
            }
        )
        db.put(channel)

        return redirect("/channel/" + channel_name)
    else:
        return redirect("/")
    
@app.route("/delete-message/<int:id>")
@login_required
def delete_message(id):
    db.delete(db.key("message", id))
    redirect_channel = request.args.get("redirect")
    print(type(redirect_channel))
    return redirect("/channel/" + redirect_channel)

@app.route("/delete-channel/<int:id>/")
@login_required
def delete_channel(id):
    db.delete(db.key("channel", id)) 
    return redirect("/")

if __name__ == "__main__":
    context = ("ssl/server.crt", "ssl/server.key")
    app.run(debug=True, port=8080, ssl_context=context)