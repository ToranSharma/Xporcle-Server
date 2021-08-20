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
        self.poll_data = None
        self.vote_data = None
        self.quiz_queue = []
        self.queue_interval = None

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

    def initialise_live_scores(self):
        for username in self.users:
            self.users[username].live_score = {"score": 0, "finished": False, "quiz_time": 0}

    def reset_live_scores(self):
        self._live_scores = {}
        for username in self.users:
            self.users[username].live_score = None

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
            if (len(self.quiz_queue) != 0
                and self.queue_interval is not None
            ):
                # Automatically go to next quiz in queue after a wait
                async def change_quiz_after_interval():
                    for i in range(int(self.queue_interval*10)):
                        await asyncio.sleep(0.1)
                        if self.queue_interval is None:
                            # We have cancelled the auto change quiz
                            await self.broadcast({"type": "cancel_change_quiz_countdown"})
                            await self.broadcast(
                                {"type": "queue_update", "queue": self.quiz_queue, "queue_interval": self.queue_interval}
                            )
                            break;
                    else: # no break
                        await self.broadcast({"type": "change_quiz", "url": self.quiz_queue[0]["url"]})

                await self.broadcast({"type": "start_change_quiz_countdown", "countdown_length": self.queue_interval})
                asyncio.create_task(change_quiz_after_interval())

    async def update_vote_data(self, votes):
        self.vote_data["response_count"] += 1
        self.vote_data["votes"] = [existing + new_vote for (existing, new_vote) in zip(self.vote_data["votes"],votes)]
        if self.vote_data["response_count"] == self.vote_data["num_voters"]:
            await self.finish_vote()
        else:
            await self.broadcast({"type": "vote_update", "vote_data": self.vote_data.copy()})

    async def finish_vote(self):
        if self.vote_data is not None:
            self.vote_data["finished"] = True
            self.vote_data["winner"] = self.vote_data["poll"]["entries"][self.vote_data["votes"].index(max(self.vote_data["votes"]))]
            await self.broadcast({"type": "vote_update", "vote_data": self.vote_data.copy()})
            self.vote_data = None


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
    if message["type"] == "start_countdown":
        if not user.room.quiz_running:
            await user.room.broadcast({"type": "start_countdown"})
        else:
            await user.queue.put({"type": "error", "error": "Quiz still on going"})

@app.incoming_processing_step
async def start_quiz(user, message):
    if user.host and message["type"] == "start_quiz" and not user.room.quiz_running:
        user.room.initialise_live_scores()
        await user.room.broadcast({"type": "start_quiz"})
        if (
            len(user.room.quiz_queue) != 0
            and user.url in user.room.quiz_queue[0]["url"]
        ):
            user.room.quiz_queue = user.room.quiz_queue[1:]
            await user.room.broadcast(
                {"type": "queue_update", "queue": user.room.quiz_queue, "queue_interval": user.room.queue_interval}
            )

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
        await user.room.send_to_hosts(suggestion_message)

''' Poll handling '''
@app.incoming_processing_step
async def poll_update(user, message):
    if (
        user.host
        and (
            message["type"] == "poll_create"
            or message["type"] == "poll_data_update"
        )
    ):
        user.room.poll_data = message["poll_data"]
        await user.room.send_to_hosts(message.copy())

@app.incoming_processing_step
async def poll_start(user, message):
    if user.host and message["type"] == "poll_start":
        user.room.vote_data = {
            "start_time": message["start_time"],
            "duration": user.room.poll_data["duration"],
            "response_count": 0,
            "num_voters": len(user.room.users),
            "poll": {
                "entries": user.room.poll_data["entries"],
                "start_time": message["start_time"],
                "duration": user.room.poll_data["duration"]
            },
            "votes": [0 for entry in user.room.poll_data["entries"]],
            "finished": False,
        }
        user.room.poll_data = None
        await user.room.broadcast({"type": "poll_start", "vote_data": user.room.vote_data})

        async def end_poll_after_duration(duration, room):
            await asyncio.sleep(duration)
            await room.finish_vote()

        asyncio.create_task(end_poll_after_duration(user.room.vote_data["duration"], user.room))

@app.incoming_processing_step
async def poll_vote(user, message):
    if message["type"] == "poll_vote":
        votes = message["votes"]
        await user.room.update_vote_data(votes);

@app.incoming_processing_step
async def add_to_queue(user, message):
    if user.host and message["type"] == "add_to_queue":
        user.room.quiz_queue.append(message["quiz"])
        await user.room.broadcast(
            {"type": "queue_update", "queue": user.room.quiz_queue, "queue_interval": user.room.queue_interval}
        )

@app.incoming_processing_step
async def reorder_queue(user, message):
    if user.host and message["type"] == "reorder_queue":
        reordered_queue = [quiz for quiz in user.room.quiz_queue if quiz["url"] != message["quiz"]["url"]]
        reordered_queue = reordered_queue[:message["index"]] + [message["quiz"]] + reordered_queue[message["index"]:]
        user.room.quiz_queue = reordered_queue;
        await user.room.broadcast(
            {"type": "queue_update", "queue": user.room.quiz_queue, "queue_interval": user.room.queue_interval}
        )

@app.incoming_processing_step
async def remove_from_queue(user, message):
    if user.host and message["type"] == "remove_from_queue":
        user.room.quiz_queue = [quiz for quiz in user.room.quiz_queue if quiz["url"] != message["quiz"]["url"]]
        await user.room.broadcast(
            {"type": "queue_update", "queue": user.room.quiz_queue, "queue_interval": user.room.queue_interval}
        )

@app.incoming_processing_step
async def change_queue_interval(user, message):
    if user.host and message["type"] == "change_queue_interval":
        user.room.queue_interval = message["queue_interval"]
        await user.room.broadcast(
            {"type": "queue_update", "queue": user.room.quiz_queue, "queue_interval": user.room.queue_interval}
        )

def calculatePoints(rankings):
    '''
    Taking points from Mario Kart 8 system:
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
async def join_room_add_quiz_queue(user, message):
    if message["type"] == "join_room" and message["success"]:
        message["queue"] = user.room.quiz_queue
        message["queue_interval"] = user.room.queue_interval

@app.outgoing_processing_step
async def users_update_add_scores(user, message):
    if message["type"] == "users_update":
        message["scores"] = user.room.scores
        
@app.outgoing_processing_step
async def host_promotion_add_urls(user, message):
    if message["type"] == "host_promotion":
        message["urls"] = user.room.urls

@app.outgoing_processing_step
async def host_promotion_add_poll_data(user, message):
    if message["type"] == "host_promotion":
        message["poll_data"] = user.room.poll_data

@app.outgoing_processing_step
async def host_promotion_add_quiz_queue(user, message):
    if message["type"] == "host_promotion":
        message["queue"] = user.room.quiz_queue
        message["queue_interval"] = user.room.queue_interval

@app.outgoing_processing_step
async def save_room_add_scores(user, message):
    if message["type"] == "save_room":
        message["save_data"]["scores"] = user.room.scores


''' DEBUGGING
@app.incoming_processing_step
async def log_incoming(user, message):
    print("Recieved from {}: {}".format(user.username,message), flush=True)

@app.outgoing_processing_step
async def log_outgoing(user, message):
    print("Sending to {}: {}".format(user.username,message), flush=True)
'''

app.websocket_rooms_route("/xporcle")

