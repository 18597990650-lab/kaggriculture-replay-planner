"""Download one explicit Kaggle replay using the user's normal Kaggle credentials."""
import argparse
import hashlib
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--episode', type=int, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    if args.out.exists():
        raise FileExistsError(args.out)
    from kaggle.api.kaggle_api_extended import KaggleApi
    from kagglesdk.competitions.types.competition_api_service import ApiGetEpisodeReplayRequest
    api = KaggleApi()
    api.authenticate()
    client = api.build_kaggle_client()
    request = ApiGetEpisodeReplayRequest()
    request.episode_id = args.episode
    response = client.competitions.competition_api_client.get_episode_replay(request)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    api.download_file(response, str(args.out), client.http_client(), quiet=True)
    raw = args.out.read_bytes()
    replay = json.loads(raw)
    if int(replay['info']['EpisodeId']) != args.episode:
        raise ValueError('Downloaded episode ID mismatch')
    print(json.dumps(dict(episode=args.episode, bytes=len(raw),
                         sha256=hashlib.sha256(raw).hexdigest())))


if __name__ == '__main__':
    main()

