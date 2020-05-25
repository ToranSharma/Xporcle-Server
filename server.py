from quart import Quart, request, websocket
import random, string, json, asyncio

app = Quart(__name__)

rooms = dict()

@app.route("/sporcle")
async def hello_world():
	return "This URL is used for websocket communications with the <a href='https://github.com/ToranSharma/Xporcle-Server'>Xporcle Server</a> for <a href='https://github.com/ToranSharma/Xporcle-Extension'>Xporcle, the sporcle multiplayer browser extension</a>\n"

@app.websocket("/sporcle")
async def ws():
	code = None
	username = None
	url = None
	queue = asyncio.Queue()

	async def processMessages(queue):
		nonlocal code, username, url
		while True:
			raw_data = await websocket.receive()
			message = json.loads(raw_data)

			response = None

			message_type = message["type"]
			
			if message_type == "create_room":
				username = message["username"]
				url = message["url"]
				code = await createRoom(username, queue, url)
				response = {"type": "scores_update", "scores": rooms[code]["scores"]}

			elif message_type == "close_room" and code in rooms:
				await closeRoom(code)
				break

			elif message_type == "join_room":
				username = message["username"]
				code = message["code"]
				url = message["url"]
				await joinRoom(code, username, queue, url)

			elif message_type == "leave_room":
				await broadcastToRoom(code, {"type": "removed_from_room", "username": username})
				await removeFromRoom(code, username)
				break

			elif message_type == "host_promotion":
				await makeHost(code, message["username"])

			elif message_type == "change_host":
				await changeHost(code, username, message["username"])

			elif message_type == "url_update":
				url = message["url"]
				await updateUrl(code, username, url)

			elif message_type == "start_quiz":
				await startQuiz(code)

			elif message_type == "live_scores_update":
				await updateLiveScores(code, username, message["current_score"], message["finished"], message["quiz_time"])

			elif message_type == "page_disconnect":
				await updateLiveScores(code, username, None, True, None)

			elif message_type == "change_quiz":
				await broadcastToRoom(code, {"type": "change_quiz", "url": message["url"]})

			elif message_type == "suggest_quiz":
				message["username"] = username
				await sendToHosts(code, message)

			else:
				response = {"type": "error", "error": "unknown message type"}

			if response is not None:
				await queue.put(response)

	async def sendMessages(queue):
		nonlocal username
		while True:
			to_send = await queue.get()
			await websocket.send(json.dumps(to_send))

	sender = asyncio.create_task(sendMessages(queue))
	reciever = asyncio.create_task(processMessages(queue))
	try:
		await asyncio.gather(sender, reciever)
	except asyncio.CancelledError:
		await broadcastToRoom(code, {"type": "removed_from_room", "username": username})
		await removeFromRoom(code, username)

async def createRoom(username, queue, url):
	code = "".join(random.choices(string.ascii_letters + string.digits, k=8))
	while code in rooms:
		code = "".join(random.choices(string.ascii_letters + string.digits, k=8))

	rooms[code] = {
		"players": {username: {"message_queue": queue, "host": True, "url": url}},
		"scores": {username: {"score": 0, "wins": 0}},
		"live_scores": {}
	}
	
	print("There " + ("are" if len(rooms) != 1 else "is") + " now {0} room".format(len(rooms)) + ("s" if len(rooms) != 1 else ""), flush=True)
	await queue.put({"type": "new_room_code", "room_code": code})
	return code

async def joinRoom(code, username, queue, url):
	success = True
	fail_reason = ""
	if code in rooms:
		if username not in rooms[code]["players"]:
			rooms[code]["players"].update({username: {"message_queue": queue, "host": False, "url": url}})
			rooms[code]["scores"].update({username: {"score": 0, "wins": 0}})
		else:
			success = False
			fail_reason = "username taken"

	else:
		fail_reason = "invalid code"
		success = False

	message = {"type": "join_room", "success": success}
	if not success:
		message.update({"fail_reason": fail_reason})
	else:
		message["hosts"] = [username for username, data in rooms[code]["players"].items() if data["host"]]

	await queue.put(message)
	await broadcastToRoom(code, {"type": "scores_update", "scores": rooms[code]["scores"]})
	await sendToHosts(code, {"type": "url_update", "username": username, "url": url})

async def closeRoom(code):
	message = {"type": "room_closed", "room_code": code}
	await broadcastToRoom(code, message)
	del rooms[code]

async def removeFromRoom(code, username):
	host = False
	if code in rooms:
		if username in rooms[code]["players"]:
			host = rooms[code]["players"][username]["host"]
			del rooms[code]["scores"][username]
			del rooms[code]["players"][username]
			# Don't delete from the live scores, they will be removed when it is cleaned up at the end of the quiz
			# But do set their finished state to true so the quiz can end.
			if (username in rooms[code]["live_scores"]):
				await updateLiveScores(code, username, None, True, None)

		
		if len(rooms[code]["players"]) == 0:
			# No one left in the room, so let's delete it.
			del rooms[code]
			print("There " + ("are" if len(rooms) != 1 else "is") + " now {0} room".format(len(rooms)) + ("s" if len(rooms) != 1 else ""), flush=True)
		else:
			# Still some players connected to the room, so send a score update to show the player being removed.
			await broadcastToRoom(code, {"type": "scores_update", "scores": rooms[code]["scores"]})
			
			if host:
				# Removed player was a host
				await hostsUpdate(code)

				# If they were the only host, then pick a new one
				hosts_remaining = True in [player["host"] for player in rooms[code]["players"].values()]
				if not hosts_remaining:
					# Choose a random player, should be good enough to just pick the first player in rooms[code]["players"].keys()
					await makeHost(code, list(rooms[code]["players"].keys())[0])

async def makeHost(code, username):
	player = rooms[code]["players"][username]
	player["host"] = True
	urls = {username: data["url"] for username, data in rooms[code]["players"].items()}
	await player["message_queue"].put({"type": "host_promotion", "urls": urls})
	await hostsUpdate(code)

async def removeHost(code, username):
	player = rooms[code]["players"][username]
	player["host"] = False
	await hostsUpdate(code)

async def changeHost(code, old, new):
	await makeHost(code, new)
	await removeHost(code, old)

async def hostsUpdate(code):
	hosts = [username for username, data in rooms[code]["players"].items() if data["host"]]
	await broadcastToRoom(code, {"type": "hosts_update", "hosts": hosts})

async def broadcastToRoom(code, message):
	if code in rooms:
		players = rooms[code]["players"]
		for player in players.values():
			await player["message_queue"].put(message)

async def sendToHosts(code, message):
	hosts = {key: value for key, value in rooms[code]["players"].items() if value["host"]}
	for host in hosts.values():
		await host["message_queue"].put(message)

async def startQuiz(code):
	if len(rooms[code]["live_scores"]) == 0:
		for username, data in rooms[code]["players"].items():
			rooms[code]["live_scores"][username] = {"score": 0, "finished": False, "quiz_time": 0}
		
		await broadcastToRoom(code, {"type": "start_quiz"})
	else:
		# Quiz already started don't do anything
		pass

async def updateLiveScores(code, username, current_score, finished, quiz_time):
	if finished:
		# Finished so don't update the time or score
		rooms[code]["live_scores"][username]["finished"] = finished
	else:
		# Not finished so update the score and time
		rooms[code]["live_scores"][username] = {"score": current_score, "finished": finished, "quiz_time": quiz_time}

	await broadcastToRoom(code, {"type": "live_scores_update", "live_scores": rooms[code]["live_scores"]})
	if finished:
		finished_states = [username["finished"] for username in rooms[code]["live_scores"].values()]
		still_playing = False in finished_states # Test if any of the finished_states are False
		if not still_playing:
			# No players left playing, so we can score the quiz
			await broadcastToRoom(code, {"type": "quiz_finished"})
			await updateScores(code)
			# Reset all the live scores ready for the next time startQuiz is called
			rooms[code]["live_scores"] = {}

async def updateScores(code):
	# Allocate points based on the live_scores

	score_list = [(username, quiz_data["score"], quiz_data["quiz_time"]) for username, quiz_data in rooms[code]["live_scores"].items()]
	score_list.sort(key = lambda entry: entry[2]) # sort by quiz_time fist
	score_list.sort(key = lambda entry: entry[1], reverse = True) # sort by score next

	# Inrement number of wins for the winner.
	winner = score_list[0][0]
	if winner in rooms[code]["scores"]:
		rooms[code]["scores"][winner]["wins"] += 1
	
	rankings = [entry[0] for entry in score_list]

	points = calculatePoints(rankings)

	for username, num_points in points.items():
		if username in rooms[code]["scores"]:
			rooms[code]["scores"][username]["score"] += num_points
		else:
			# The player left during the quiz, don't give them points
			pass
	
	await broadcastToRoom(code, {"type": "scores_update", "scores": rooms[code]["scores"]})

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
		points = points[ 12 - num_players : ]
	
	return dict(zip(rankings, points))
	
async def updateUrl(code, username, url):
	rooms[code]["players"][username]["url"] = url
	await sendToHosts(code, {"type": "url_update", "username": username, "url": url})








