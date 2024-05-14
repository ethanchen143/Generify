from flask import Flask, redirect, request, session, url_for, render_template,jsonify
from spotipy.oauth2 import SpotifyOAuth
import spotipy
from datetime import timedelta,datetime
import redis
import json
from uuid import uuid4
import numpy as np
import os
import threading

app = Flask(__name__)
app.config['SESSION_COOKIE_NAME'] = 'spotify_login_session'
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['REMEMBER_COOKIE_SECURE'] = False
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(minutes=30)
app.secret_key = os.getenv('SECRET_KEY', 'secret_key')

REDIRECT_URL = f"https://www.genrify.us/callback"
SCOPE = 'user-library-read playlist-read-private playlist-modify-private playlist-modify-public'

from urllib.parse import urlparse
redis_url = urlparse(os.getenv('REDISCLOUD_URL'))
redis_client = redis.Redis(host=redis_url.hostname, port=redis_url.port, password=redis_url.password)

@app.after_request
def add_header(response):
    response.cache_control.no_store = True
    return response

@app.route('/')
def index():
    session['uuid'] = str(uuid4())
    logged_in = 'token_info' in session and 'access_token' in session['token_info']
    sp_oauth = SpotifyOAuth(
        client_id=os.getenv('SPOTIPY_CLIENT_ID'),
        client_secret=os.getenv('SPOTIPY_CLIENT_SECRET'),
        redirect_uri=REDIRECT_URL,
        scope=SCOPE,
        cache_path=f"cache/.cache-{session['uuid']}"
    )
    auth_url = sp_oauth.get_authorize_url()
    return render_template('index.html', login_url=auth_url, logged_in=logged_in)

@app.route('/callback')
def callback():
    session['uuid'] = str(uuid4())
    sp_oauth = SpotifyOAuth(
        client_id=os.getenv('SPOTIPY_CLIENT_ID'),
        client_secret=os.getenv('SPOTIPY_CLIENT_SECRET'),
        redirect_uri=REDIRECT_URL,
        scope=SCOPE,
        cache_path=f"cache/.cache-{session['uuid']}"
    )
    token_info = sp_oauth.get_access_token(request.args.get('code'))
    session['token_info'] = token_info
    access_token = token_info['access_token']
    if sp_oauth.is_token_expired(token_info):
        token_info = sp_oauth.refresh_access_token(token_info['refresh_token'])
        session['token_info'] = token_info
        access_token = token_info['access_token']
    sp = spotipy.Spotify(auth=access_token)
    user_profile = sp.current_user()
    session['user_id'] = user_profile['id']
    return redirect(url_for('index'))

@app.route('/logout')
def logout():
    redis_client.flushdb()
    session.pop('token_info', None)
    session.clear()
    return redirect('https://accounts.spotify.com/en/logout')

def simplify_data(data):
    return [
        {
            'added_at': entry['added_at'][:10],
            'album_name': track['album']['name'],
            'album_release_date': track['album']['release_date'],
            'artist_names': ', '.join(artist['name'] for artist in track['artists']),
            'artist_id': track['artists'][0]['id'],
            'track_name': track['name'],
            'track_popularity': track['popularity'],
            'id': track['id']
        } for entry in data for track in [entry['track']]
    ]

def enrich_data(data, sp):
    chunk_size = 50  # Spotify API batch limit
    for i in range(0, len(data), chunk_size):
        track_chunk = data[i:i+chunk_size]
        artist_ids = [track['artist_id'] for track in track_chunk]
        artists = sp.artists(artist_ids)['artists']
        features = sp.audio_features([track['id'] for track in track_chunk])
        for track, artist, feature in zip(track_chunk, artists, features):
            track['genres'] = artist.get('genres', [])
            feature_data = {k: feature.get(k, 0) for k in [
                'acousticness', 'danceability', 'energy', 'instrumentalness', 
                'liveness', 'loudness', 'speechiness', 'tempo', 'valence', 
                'key', 'mode', 'time_signature']}
            track.update(feature_data)
        
def bg_get_tracks(user_id,sp):
    if not redis_client.exists(user_id):
        offset = 0
        limit = 50
        all_tracks = []
        while True:
            results = sp.current_user_saved_tracks(limit=limit, offset=offset)
            all_tracks.extend(results['items'])
            if results['next'] is None:
                break
            offset += limit
        redis_client.setex(user_id, 900, json.dumps(all_tracks))
    
def bg_analyze_tracks(user_id,sp):
    ana_id = user_id + 'AN'
    if not redis_client.exists(ana_id):
        tracks = json.loads(redis_client.get(user_id).decode('utf-8'))
        data = simplify_data(tracks)
        enrich_data(data, sp)
        from genre_map import convert
        for track in data:
            processed = list(set(convert(g) for g in track['genres'])) # Niche Genres-> Mainstream Genres
            if 'Others' in processed and len(processed) != 1:
                processed.remove('Others')
            track['genres'] = processed
        redis_client.setex(ana_id, 900, json.dumps(data))
    from analysis import analyze
    an_text_id = user_id + 'AN-Text'
    if not redis_client.exists(an_text_id):
        ana_text = analyze(data)
        redis_client.setex(an_text_id, 900, json.dumps(ana_text))

import faiss
def clustering(data, k, max_iterations=500):
    num_clusters = k
    dimension = data.shape[1]
    initial_centroids = np.random.rand(num_clusters, dimension).astype('float32')
    kmeans = faiss.Kmeans(dimension, num_clusters, niter=max_iterations, verbose=True)
    kmeans.centroids = initial_centroids  # Initialize centroids
    kmeans.train(data)
    D, I = kmeans.index.search(data, 1)
    return I.flatten(), kmeans.centroids
        
def bg_organize_tracks(user_id,sp):
    dates_weight = 5
    genre_weights = 1
    popular_weights,valence_weights,energy_weights,dance_weights,acoustic_weights = 2.5,2.5,2.5,2.5,2.5
    ana_id = user_id + 'AN'
    bg_analyze_tracks(user_id,sp)   
    data = json.loads(redis_client.get(ana_id).decode('utf-8'))
    for track in data:
        order = ["Soundtracks", "Classical", "Experimental", "Jazz", "Country/Folk", "Funk", "Indie", "Rock", "RnB/Soul", "Hip-Hop", "Electronic", "Pop", "Others"]
        order = [genre for genre in order if genre in track['genres']]
        track['genres'] = order[0] if order else 'Others'
    
    dates = []
    for d in data:
        try:
            if len(d['album_release_date']) == 10:
                dates.append(datetime.strptime(d['album_release_date'], '%Y-%m-%d'))
            elif len(d['album_release_date']) == 7:
                dates.append(datetime.strptime(d['album_release_date'], '%Y-%m'))
            else:
                dates.append(datetime.strptime(d['album_release_date'], '%Y'))
        except:
            dates.append(datetime.now())
    min_date = min(dates)
    max_date = max(dates)
    dates = [((date - min_date).total_seconds() / (max_date - min_date).total_seconds()) * dates_weight for date in dates]
    
    genre_score = {
        "Soundtracks": 0, "Classical": 10, "Jazz": 20, "Country/Folk": 40,
        "RnB/Soul": 60, "Pop": 80, "Funk": 100, "Indie": 120, "Rock": 140,
        "Hip-Hop": 160, "Electronic": 180, "Experimental": 200, "Others": 250
    }
    genres = [genre_score[track['genres']] * genre_weights for track in data]
    genres = [250 if np.isnan(x) else x for x in genres]
    
    popularities = [data['track_popularity'] / 100 for data in data]

    normalized_data = np.array([
        [ dates[idx],genres[idx],popularities[idx] * popular_weights,
            data[idx]['valence'] * valence_weights,
            data[idx]['danceability'] * dance_weights,
            data[idx]['energy'] * energy_weights,
            data[idx]['acousticness'] * acoustic_weights] for idx in range(len(data))
    ])

    num_k = len(data) // 30 + 1
    cluster_ids, centroids = clustering(normalized_data, k=num_k)
    
    from collections import defaultdict
    cluster_tracks = defaultdict(list)
    for idx, track in enumerate(data):
        cluster_id = int(cluster_ids[idx])  
        cluster_tracks[cluster_id].append(track['id'])

    def custom_name(cluster_id):
        avg_date = min_date + (centroids[cluster_id][0] / dates_weight) * (max_date - min_date)
        year = avg_date.strftime("%Y")
        genre_num = int(centroids[cluster_id][1])
        closest_genre = None
        min_difference = float('inf')
        for k, v in genre_score.items():
            difference = abs(genre_num - v)
            if difference < min_difference:
                min_difference = difference
                closest_genre = k
        res = f"{year}'s {closest_genre}"
        return res
    
    playlist_ids = {}
    for cluster_id in cluster_tracks:
        playlist_name = f'Genrify_{cluster_id + 1}_{custom_name(cluster_id)}'
        playlist = sp.user_playlist_create(user=user_id, name=playlist_name, public=False)
        playlist_ids[cluster_id] = playlist['id']

    for cluster_id, tracks in cluster_tracks.items():
        batch_size = 50 # Spotify Rate Limit
        for i in range(0, len(tracks), batch_size):
            batch_tracks = tracks[i:i + batch_size]
            sp.playlist_add_items(playlist_ids[cluster_id], batch_tracks)

def bg_delete_playlists(user_id,sp):
    playlists = sp.current_user_playlists()
    to_delete = []
    while playlists:
        for playlist in playlists['items']:
            if playlist['name'].startswith('Genrify'):
                to_delete.append(playlist['id'])
        if playlists['next']:
            playlists = sp.next(playlists)
        else:
            playlists = None
    batch_size = 100 
    for i in range(0, len(to_delete), batch_size):
        batch = to_delete[i:i + batch_size]
        for playlist_id in batch:
            sp.user_playlist_unfollow(user_id, playlist_id)
                    
def background_job(user_id, token_info, job_type):
    sp = spotipy.Spotify(auth=token_info['access_token'])
    try:
        if job_type == 'get_tracks':
            bg_get_tracks(user_id,sp)                
        elif job_type == 'analyze_tracks':
            bg_analyze_tracks(user_id,sp)                
        elif job_type == 'organize_tracks':
            bg_organize_tracks(user_id,sp)
        elif job_type == 'delete_playlists':
            bg_delete_playlists(user_id,sp)
        redis_client.set(f"{user_id}_status", 'completed')
    except Exception as e:
        redis_client.set(f"{user_id}_status", f'error: {str(e)}')

@app.route('/start_task/<job_type>')
def start_task(job_type):
    if 'token_info' not in session or 'access_token' not in session['token_info']:
        return redirect(url_for('index'))
    
    user_id = session['user_id']
    token_info = session['token_info']
    redis_client.set(f"{user_id}_status", 'pending')
    thread = threading.Thread(target=background_job, args=(user_id, token_info, job_type))
    thread.start()
    return render_template('waiting.html', job_type=job_type)

@app.route('/get_tracks')
def get_tracks():
    return redirect(url_for('start_task', job_type='get_tracks'))

@app.route('/analyze_tracks')
def analyze_tracks():
    return redirect(url_for('start_task', job_type='analyze_tracks'))

@app.route('/organize_tracks')
def organize_tracks():
    return redirect(url_for('start_task', job_type='organize_tracks'))

@app.route('/delete_playlists')
def delete_playlists():
    return redirect(url_for('start_task', job_type='delete_playlists'))

@app.route('/check_status')
def check_status():
    user_id = session['user_id']
    status = redis_client.get(f"{user_id}_status").decode('utf-8')
    if status == 'completed':
        job_type = request.args.get('job_type')
        print(f'checking status, job_type: {job_type}')
        return jsonify({'status': 'completed', 'job_type': job_type})
    elif 'error' in status:
        return jsonify({'status': 'error', 'details': status})
    else:
        return jsonify({'status': 'pending'})

@app.route('/results')
def results():
    user_id = session['user_id']
    job_type = request.args.get('type')
    if job_type == 'get_tracks':
        all_tracks = redis_client.get(user_id)
        if all_tracks:
            all_tracks = json.loads(all_tracks.decode('utf-8'))
            return render_template('dashboard.html', tracks=all_tracks)
        else:
            return render_template('message.html', text="No tracks found.")
    elif job_type == 'analyze_tracks':
        data = redis_client.get(user_id + 'AN')
        ana_text = redis_client.get(user_id + 'AN-Text')
        if data and ana_text:
            data = json.loads(data.decode('utf-8'))
            ana_text = json.loads(ana_text.decode('utf-8'))
            return render_template('analytics.html', data=data, text=ana_text)
        else:
            return render_template('message.html', text="Analysis data not found.")
    elif job_type == 'organize_tracks':
        return render_template('message.html', text="Genrify_Playlists created, check them out on your Spotify app!")
    elif job_type == 'delete_playlists':
        return render_template('message.html',text = "Genrify_Playlists deleted, you can log out and generate again.")
    else:
        return render_template('message.html', text=f"Invalid job type: {job_type}")

if __name__ == '__main__':
    app.run(debug=True)