import logging
from plexapi.server import PlexServer
from plexapi.video import Episode

def connect_to_plex(plex_url, plex_token, timeout=10.0):
    """
    Connects to the Plex Media Server using URL and API Token.
    """
    try:
        plex = PlexServer(plex_url, plex_token, timeout=timeout)
        logging.info(f"Successfully connected to Plex server at: {plex_url}")
        return plex
    except Exception as e:
        logging.error(f"Error connecting to Plex server at {plex_url}: {e}")
        raise

def get_next_episodes_for_session(plex, session, count=1):
    """
    Identifies the next `count` episodes for a given Episode playback session.
    Retrieves all episodes for the associated show and filters for ones appearing
    sequentially after the current episode (including crossing into subsequent seasons).
    """
    if not isinstance(session, Episode):
        return []
        
    try:
        # Fetch the entire show either via direct rating key or library search fallback
        if hasattr(session, 'grandparentRatingKey') and session.grandparentRatingKey:
            show = plex.fetchItem(session.grandparentRatingKey)
            all_episodes = show.episodes()
        else:
            show_title = session.grandparentTitle
            section_title = session.librarySectionTitle
            show = plex.library.section(section_title).get(show_title)
            all_episodes = show.episodes()
            
        current_season_num = session.parentIndex or 0
        current_episode_num = session.index or 0
        
        # Filter for upcoming episodes (future seasons, or current season with higher index)
        next_episodes = []
        for ep in all_episodes:
            ep_season = ep.seasonNumber or 0
            ep_index = ep.index or 0
            if (ep_season > current_season_num) or (ep_season == current_season_num and ep_index > current_episode_num):
                next_episodes.append(ep)
                
        # Sort by season number and episode number index, handling potential None values safely
        next_episodes.sort(key=lambda ep: (ep.seasonNumber or 0, ep.index or 0))
        
        logging.debug(f"Found {len(next_episodes)} total upcoming episodes for show '{session.grandparentTitle}'. Request limit: {count}")
        return next_episodes[:count]
    except Exception as e:
        logging.error(f"Error finding next episodes for session S{session.parentIndex}E{session.index}: {e}")
        return []
