import nonebot
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter
from dotenv import load_dotenv


def main() -> None:
    load_dotenv()
    nonebot.init()
    driver = nonebot.get_driver()
    driver.register_adapter(OneBotV11Adapter)
    nonebot.load_plugin("nonebot_plugin_apscheduler")
    nonebot.load_plugins("plugins")
    nonebot.run()


if __name__ == "__main__":
    main()
