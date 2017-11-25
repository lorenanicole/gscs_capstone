import os.path, sys
sys.path.append(os.path.join(os.path.dirname(os.path.realpath(__file__)), os.pardir))

from crontab import CronTab

PARENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE_DIR = os.path.dirname(os.path.realpath(__file__))

def main():
    users_cron = CronTab(user='root')

    job = users_cron.new(command='cd {0}/{1}; python34 '.format(PARENT_DIR, 'scrapers/reddit/reddit'))
    job.setall('00 22 * * *')
    job.enable()

    users_cron.write()
    print(users_cron.render())

if __name__ == "__main__":
    main()