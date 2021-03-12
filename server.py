from quart_websocketrooms import WebSocketRooms, User, Room
import json
import asyncio


class User(User):
    def __init__(self, *args, **kargs):
        super().__init__()
        self.url = None
        self.score = {"score": 0, "wins": 0}
        self.live_score = None #{"score": int, "finished": bool, "quiz_time": int}


class Room(Room):
    def __init__(self, *args, **kargs):
        super().__init__(*args, **kargs)
        self._live_scores = {}

    def add_user(self, user):
        added = super().add_user(user)
        if self.loaded:
            if user.username in self.save_data["scores"]:
                user.score = self.save_data["scores"][user.username]
        return added

    @property
    def urls(self):
        return {username: self.users[username].url for username in self.users}

    @property
    def scores(self):
        return {username: self.users[username].score for username in self.users}

    @scores.setter
    def scores(self, new_scores):
        for username in new_scores:
            if username in self.users:
                self.users[username].score = new_scores[username]

    @property
    def live_scores(self):
        self._live_scores.update({username: self.users[username].live_score for username in self.users})
        return self._live_scores
        
    def reset_live_scores(self):
        _live_scores = {}
        for username in self.users:
            self.users[username].live_score = {"score": 0, "finished": False, "quiz_time": 0}

    @property
    def quiz_running(self):
        return None not in [self.live_scores[username] for username in self.users]

    @property
    def quiz_finished(self):
        return False not in [self.live_scores[username]["finished"] for username in self.users]

    async def update_scores(self):
        score_list = [(username, quiz_data["score"], quiz_data["quiz_time"]) for username, quiz_data in self.live_scores.items()]
        score_list.sort(key = lambda entry: entry[2]) # sort by quiz_time fist
        score_list.sort(key = lambda entry: entry[1], reverse = True) # sort by score next

        # Increment number of wins for the winner.
        winner = score_list[0][0]
        if winner in self.scores:
            self.scores[winner]["wins"] += 1
        
        rankings = [entry[0] for entry in score_list]

        points = calculatePoints(rankings)

        for username, num_points in points.items():
            if username in self.users:
                self.scores[username]["score"] += num_points
            else:
                # The player left during the quiz, don't give them points
                pass
        
        self.reset_live_scores()
        await self.broadcast({"type": "scores_update", "scores": self.scores})

    async def send_live_scores_update(self):
        await self.broadcast({"type": "live_scores_update", "live_scores": self.live_scores})
        if self.quiz_finished:
            await self.broadcast({"type": "quiz_finished"})
            await self.update_scores()


app = WebSocketRooms(__name__, CustomRoomClass=Room, CustomUserClass=User)

@app.route("/xporcle")
async def hello_world():
    return "This URL is used for websocket communications with the <a href='https://github.com/ToranSharma/Xporcle-Server'>Xporcle Server</a> for <a href='https://github.com/ToranSharma/Xporcle-Extension'>Xporcle, the sporcle multiplayer browser extension</a>\n"

@app.incoming_processing_step
async def url_update(user, message):
    if message["type"] == "url_update":
        url = message["url"]
        user.url = url
        await user.room.send_to_hosts({"type": "url_update", "username": user.username, "url": url})

@app.incoming_processing_step
async def live_scores_update(user, message):
    if message["type"] == "live_scores_update":
        if message["finished"]:
            user.live_score["finished"] = True
        else:
            user.live_score["score"] = message["current_score"]
            user.live_score["quiz_time"] = message["quiz_time"]
        await user.room.send_live_scores_update()

@app.incoming_processing_step
async def start_countdown(user, message):
    if message["type"] == "start_coutdown":
        user.room.broadcast({"type": "start_countdown"})

@app.incoming_processing_step
async def start_quiz(user, message):
    if message["type"] == "start_quiz" and not user.room.quiz_running:
        user.room.reset_live_scores()
        await user.room.broadcast({"type": "start_quiz"})

@app.incoming_processing_step
async def page_disconnect(user, message):
    if message["type"] == "page_disconnect":
        user.room.live_scores[user.username]["finished"] = True
        await user.room.send_live_scores_update()

@app.incoming_processing_step
async def change_quiz(user, message):
    if message["type"] == "change_quiz":
        await user.room.broadcast({"type": "change_quiz", "url": message["url"]})

@app.incoming_processing_step
async def suggest_quiz(user, message):
    if message["type"] == "suggest_quiz":
        suggestion_message = message.copy()
        suggestion_message["username"] = user.username
        user.room.send_to_hosts(suggestion_message)

def calculatePoints(rankings):
    '''
    Taking points for Mario Kart 8 system:
    https://mariokart.fandom.com/wiki/Driver%27s_Points#Mario_Kart_8_and_Mario_Kart_8_Deluxe
    Points awared for first 12 places
    1st: 15, 2nd: 12, 3rd: 10, 4th-12th: 9 ... 1, >13th: 0
    If fewer than 12 players, points awarded from 1 upward.
    '''
    points = [15, 12, 10, 9, 8, 7, 6, 5, 4, 3, 2, 1]
    num_players = len(rankings)
    
    if num_players > 12:
        zeros = [0] * (num_players - 12)
        points.extend(zeros)
    else:
        points = points[ 12 - num_players:]
    
    return dict(zip(rankings, points))


@app.outgoing_processing_step
async def join_room_add_hosts(user, message):
    if message["type"] == "join_room" and message["success"]:
        message["hosts"] = list(user.room.hosts)

@app.outgoing_processing_step
async def users_update_add_scores(user, message):
    if message["type"] == "users_update":
        message["scores"] = user.room.scores
        
@app.outgoing_processing_step
async def host_promotion_add_urls(user, message):
    if message["type"] == "host_promotion":
        message["urls"] = user.room.urls

@app.outgoing_processing_step
async def log(user, message):
    print("Sending to {}: {}".format(user.username,message), flush=True)

app.websocket_rooms_route("/xporcle")
