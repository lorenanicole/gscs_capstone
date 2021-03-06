"""
Python script to scrape Reddit: http://praw.readthedocs.io/en/latest/getting_started/quick_start.html
The /learnpython subreddit recommends PRAW: https://www.reddit.com/r/learnpython/comments/574pn5/anyone_have_a_reddit_scraper/.
"""
from datetime import datetime
import json
import logging
from logging.handlers import RotatingFileHandler
import boto3
import io
import praw
import os
import yaml
from pathlib import Path
from collections import namedtuple

from praw.models import Submission, Redditor, Subreddit, MoreComments
from praw.models.comment_forest import CommentForest

__author__ = 'lorenamesa'


PARENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE_DIR = os.path.dirname(os.path.realpath(__file__))
CONFIG = yaml.load(open(PARENT_DIR + '/config.yml', 'r'))
REDDIT_CONFIG = CONFIG.get('reddit')
SUBREDDITS = ['Leagueoflegends', 'AskReddit', 'soccer']

logger = logging.getLogger('reddit_comments_cron')
logger.setLevel(20)
handler = RotatingFileHandler(CONFIG.get('cron').get('reddit_comment_log_path'),
                              maxBytes=10*1024*1024,
                              backupCount=5)
logger.addHandler(handler)

Transformer = namedtuple('Transformer', ['source', 'field', 'func'])

class ParsedRedditInfo(object):

    DELETE_PROPS = [
        '_reddit', '_comments', '_comments_by_id'
    ]

    REDDIT_MAP_TO_FLAT = {
        '_comments': Transformer(CommentForest, '_comments', len),
        '_submission': Transformer(Submission, 'id', None),
        '_replies': Transformer(CommentForest, '_comments', len),
        'author': Transformer(Redditor, 'name', None),
        'subreddit': Transformer(Subreddit, 'display_name', None),
    }

    @staticmethod
    def to_json(reddit_obj):
        reddit_obj_dict = vars(reddit_obj)

        for prop in ParsedRedditInfo.DELETE_PROPS:
            if reddit_obj_dict.get(prop):
                reddit_obj_dict.pop(prop)

        for prop, transformer in ParsedRedditInfo.REDDIT_MAP_TO_FLAT.items():
            if not reddit_obj_dict.get(prop):
                continue

            if isinstance(reddit_obj_dict.get(prop), transformer.source) and transformer.func:
                reddit_obj_dict[prop] = transformer.func(
                    getattr(reddit_obj_dict.get(prop), transformer.field)
                )
            else:
                reddit_obj_dict[prop] = getattr(reddit_obj_dict.get(prop), transformer.field)

        return reddit_obj_dict


def write_to_s3(bucket_name, bucket_key, data_format='json', base_dir=None, subreddit_data=None, date=None):
    session = boto3.session.Session(
        aws_access_key_id=CONFIG.get('aws').get('aws_access_key_id'),
        aws_secret_access_key=CONFIG.get('aws').get('aws_secret_access_key')
    )
    s3 = session.resource('s3')

    if base_dir:
        bucket = s3.Bucket(bucket_name)

        # .glob returns a generator
        # https://stackoverflow.com/questions/42246819/loop-over-results-from-path-glob-pathlib
        for filename in Path(base_dir).glob('*.{0}'.format(data_format)):
            bucket_key_name = str(filename).split('/')[-2:]
            bucket_key_name = '/'.join([s for s in bucket_key_name])
            logger.info('Uploading {0}'.format(filename))
            bucket.upload_file(str(filename), Key=bucket_key + '/{0}'.format(bucket_key_name))

        return 1

    today_date_string = datetime.today().strftime('%Y-%m-%d')
    subreddit_name = list(subreddit_data.keys())[0]
    for data_type, data_val in subreddit_data[subreddit_name].items():

        key = '{0}/{1}/{2}_{3}.{4}'.format(
            CONFIG.get('s3').get('reddit_bucket_key'),
            date if date else today_date_string,
            subreddit_name,
            data_type,
            data_format
        )

        if data_format == 'json':
            # Encode Python dict as JSON str
            data_val = json.dumps(data_val)
        fake_handle = io.StringIO(data_val)

        s3object = s3.Object(bucket_name, key)
        s3object.put(Body=fake_handle.read())  # Body must be read as a binary str
        print('Uploading {0}'.format(key))
        logger.info('Uploading {0}'.format(key))

    return 1


def main():

    reddit = praw.Reddit(client_id=REDDIT_CONFIG.get('client_id'),
                         client_secret=REDDIT_CONFIG.get('client_secret'),
                         refresh_token=REDDIT_CONFIG.get('refresh_token'),
                         user_agent=REDDIT_CONFIG.get('user_agent'))

    all_subreddit_data = dict.fromkeys(SUBREDDITS, None)

    today_date_string = datetime.today().strftime('%Y-%m-%d')

    logger.info('Starting {} reddit scrape'.format(today_date_string))

    # Download Submissions
    bucket_name = CONFIG.get('s3').get('bucket_name')
    session = boto3.session.Session(
        aws_access_key_id=CONFIG.get('aws').get('aws_access_key_id'),
        aws_secret_access_key=CONFIG.get('aws').get('aws_secret_access_key')
    )

    s3 = session.client('s3')
    bucket_keys = s3.list_objects_v2(Bucket=bucket_name)

    keys = []

    for obj in bucket_keys['Contents']:
        key = obj['Key']
        if 'politics' in key or '2017-11-28' in key or 'authors' in key or 'comments' in key:
            continue

        keys.append(obj['Key'])

    keys_to_submissions = {}
    for key in keys:
        submissions = s3.get_object(Bucket=bucket_name, Key=key)['Body']
        submissions = json.loads(submissions.read().decode('utf-8'))
        submissions = list(map(lambda s: s['permalink'], submissions))
        keys_to_submissions[key] = submissions

    subreddit_submissions_to_data = dict.fromkeys(SUBREDDITS, {})

    for key, submissions in keys_to_submissions.items():
        print('Processing key {}'.format(key))
        split_key = key.split('/')
        subreddit = split_key[-1].split('_')[0]
        date = split_key[1]

        subreddit_submissions_to_data[subreddit][date] = {'authors': [], 'comments': []}
        for submission in submissions:
            s = reddit.submission(url='https://www.reddit.com/{}'.format(submission))

            if s.author:
                subreddit_submissions_to_data[subreddit][date]['authors'].append(s.author)

            comment_queue = s.comments[:]  # Seed with top-level comments
            while comment_queue:
                # print(len(comment_queue))

                comment = comment_queue.pop(0)

                if isinstance(comment, MoreComments):
                    s.comments.replace_more(limit=0)
                    continue

                comment_queue.extend(comment.replies)

                subreddit_submissions_to_data[subreddit][date]['comments'].append(comment)

                if comment.author:
                    subreddit_submissions_to_data[subreddit][date]['authors'].append(comment.author)


        for comment_indx, comment in enumerate(subreddit_submissions_to_data[subreddit][date]['comments']):
            if not comment:
                continue

            parsed_comment = ParsedRedditInfo.to_json(comment)
            if isinstance(parsed_comment.get('_replies'), CommentForest):
                    parsed_comment['_replies'] = len(
                        getattr(parsed_comment.get('_replies'), '_comments')
                    )
            subreddit_submissions_to_data[subreddit][date]['comments'][comment_indx] = parsed_comment

        for author_indx, author in enumerate(subreddit_submissions_to_data[subreddit][date]['authors']):
            if isinstance(author, Redditor):
                parsed_author = ParsedRedditInfo.to_json(author)
                subreddit_submissions_to_data[subreddit][date]['authors'][author_indx] = parsed_author

        print('Done processing key {}'.format(key))
        write_to_s3(
            bucket_name=CONFIG.get('s3').get('bucket_name'),
            bucket_key=CONFIG.get('s3').get('reddit_bucket_key'),
            subreddit_data={subreddit: subreddit_submissions_to_data[subreddit][date]},
            date=date
        )

    logger.info('Finishing {} {} reddit comment scrape'.format(date, subreddit))

    return 1

if __name__ == '__main__':

    scraper_status = main()
    logger.info(scraper_status)
