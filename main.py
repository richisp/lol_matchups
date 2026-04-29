import argparse

from crawler import run_crawl


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Crawl ranked solo/duo matches from Riot API into a SQLite database."
    )
    parser.add_argument("seed", help="Seed player as 'GameName#TAG' (e.g. 'Faker#KR1')")
    parser.add_argument(
        "--max-players",
        type=int,
        default=20,
        help="Stop after crawling this many players (default: 20)",
    )
    parser.add_argument(
        "--matches-per-player",
        type=int,
        default=20,
        help="Match history depth per player, max 100 per page (default: 20)",
    )
    args = parser.parse_args()
    run_crawl(args.seed, args.max_players, args.matches_per_player)


if __name__ == "__main__":
    main()
