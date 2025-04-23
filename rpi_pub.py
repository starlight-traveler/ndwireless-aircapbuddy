from datetime import datetime, timedelta, timezone
import firebase
from getpass import getuser
import jc
import json
import logging
from logging import Formatter
from logging.handlers import TimedRotatingFileHandler
from paho.mqtt import client as mqtt
from pathlib import Path
import re
import time
import utils


logdir = Path(f"/home/{getuser()}/airbuddy/logs")
logpaths = {
    "mqtt": logdir / "rpi_pub.log",
    "speedtest": logdir / "speedtest_logger.log"
}

# Logging setup
handler = TimedRotatingFileHandler(
    filename=logpaths["mqtt"],
    when="D", interval=1, backupCount=90, encoding="utf-8",
    delay=False)
formatter = Formatter(
    fmt='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
logging.basicConfig(
    handlers=[handler],
    level=logging.DEBUG
)

logging.info("Script started.")


# Get eth0 MAC address
mac = "00-00-00-00-00-00"
try:
    mac = open("/sys/class/net/eth0/address").readline()[0:17].upper().replace(
        ":", "-")
except Exception as e:
    logging.error("Cannot retrieve eth0 MAC address: %s", e, exc_info=1)

logging.info("eth0 MAC address: %s", mac)


# Update config
config = firebase.read_config(mac)
logging.info("Config: %s", config)


# Publish topics
topic_report = f"Schmidt/{mac}/report/status"
# topic_report_ip = f"Schmidt/{mac}/report/status/ip"
# topic_report_mac = f"Schmidt/{mac}/report/status/mac"
topic_report_conf = f"Schmidt/{mac}/report/config"
# Subscribed topics
topic_config_all = f"Schmidt/all/config/#"
topic_config_specific = f"Schmidt/{mac}/config/#"

# Path to the saved message from last command
last_cmd = Path(".last_cmd.json")


def create_msg(msg_type, out, err=""):
    return {
        "mac": mac,
        "timestamp": datetime.now(timezone.utc).astimezone().isoformat(),
        "type": msg_type,
        "result": "failed" if err else "success",
        "out": out,
        "err": err,
    }


def get_ssid():
    output = utils.run_cmd("iwconfig")

    # Process output to extract SSID using regular expression
    pattern = r'ESSID:"([^"]*)"'
    match = re.search(pattern, output)
    if match:
        essid = match.group(1)
        ssid = f'{essid}'
    else:
        ssid = 'NONE'
    return ssid


def get_ifaces(specific=None):
    parsed = jc.parse("ifconfig", utils.run_cmd("ifconfig -a"))
    # Remove loopback
    parsed = [item for item in parsed if item["name"] != "lo"]
    # Remove unneeded parameters
    match specific:
        case "up":
            parsed = {item["name"]: "UP" in item["state"] for item in parsed}
        case "ip":
            parsed = {item["name"]: item["ipv4_addr"] for item in parsed}
        case "mac":
            parsed = {item["name"]: item["mac_addr"] for item in parsed}
        case _:
            parsed = list(map(lambda x: {
                "name": x["name"],
                "up": "UP" in x["state"],
                "ip_address": x["ipv4_addr"],
                "mac_address": x["mac_addr"]
            }, parsed))

    return parsed


def get_services():
    parsed = jc.parse("systemctl", utils.run_cmd("systemctl -a"))
    # Look for mqtt and speedtest_logger
    parsed = [item for item in parsed if (
        item["unit"] == "mqtt.service"
        or item["unit"] == "speedtest_logger.service")]

    return parsed


def create_status(specific=None):
    match specific:
        case "ssid":
            out = get_ssid()
        case "iface":
            out = get_ifaces()
        case "up":
            out = get_ifaces("up")
        case "ip":
            out = get_ifaces("ip")
        case "mac":
            out = get_ifaces("mac")
        case "srv":
            out = get_services()
        case _:
            out = {
                "ssid": get_ssid(),
                "ifaces": get_ifaces(),
                "services": get_services()
            }

    msg_type = "status"
    if specific in ["ssid", "iface", "up", "ip", "mac", "srv"]:
        msg_type += f"/{specific}"

    return create_msg(msg_type, out)


def delete_last_cmd():
    logging.info("Deleting last cmd.")
    last_cmd.unlink()


def write_last_cmd(msg):
    logging.info("Writing msg from last cmd: %s", msg)
    with open(last_cmd, "w") as file:
        json.dump(msg, file)


def restore_last_cmd(client):
    logging.info("Restoring msg from last cmd.")
    if last_cmd.is_file():
        with open(last_cmd, "r") as file:
            msg = json.load(file)
        logging.debug("Got last cmd msg: %s", msg)
        if "timestamp" in msg:
            span = datetime.now(timezone.utc) - datetime.fromisoformat(
                msg["timestamp"])
            # Check if the msg is posted less than 10 minutes ago
            if span.seconds < 600:
                logging.info("Sending last cmd reply: %s", msg)
                client.publish(topic_report_conf, json.dumps(msg), qos=1)
            else:
                logging.info("Discarding old msg with timestamp %s",
                             msg["timestamp"])
        delete_last_cmd()


def on_connect(client, userdata, flags, rc):
    if rc == 0:
        logging.info("Connected to MQTT broker")
        # Subscribe to "Schmidt/config" for commands
        client.subscribe(topic_config_all)
        client.subscribe(topic_config_specific)
    else:
        logging.error(f"Connection failed with code {rc}")


def on_message(client, userdata, msg):
    # We only receive message from "Schmidt/config" topic
    topic = msg.topic
    logging.info("Message received: %s", topic)
    splits = topic.split("/")
    [_, target, _, command] = splits[:4]
    extras = splits[4:]

    # Skip if the command is not intended for this mac
    if target != "all" and target != mac:
        logging.info("Skipping command intended for %s.", target)
        return

    match command:
        case "ping":
            # Ping the Pi
            logging.info("Got ping command")
            msg = create_msg("ping", {"pong": msg.payload.decode("utf-8")})
            logging.info("Sending reply: %s", msg)
            client.publish(topic_report_conf, json.dumps(msg), qos=1)

        case "update":
            # Run the update script
            logging.info("Got update command")
            # Write down last command, assuming successful update
            msg = create_msg("update", {"returncode": 0})
            write_last_cmd(msg)

            output = utils.run_cmd(
                ("wget -q -O - https://raw.githubusercontent.com/adstriegel/"
                 "airbuddy/main/pi-setup.sh | /bin/bash"),
                raw_out=True)
            # This is not reached if update succeeded.
            logging.debug(output)
            # In case service not restarted due to failed update
            # (or any reasons).
            delete_last_cmd()
            msg = create_msg("update", {"returncode": output["returncode"]},
                             ("" if output["returncode"] == 0
                              else output["stderr"]))
            logging.info("Sending reply: %s", msg)
            client.publish(topic_report_conf, json.dumps(msg), qos=1)

        case "status":
            # Query status
            # Extra options: "/[ssid|iface|up|ip|mac|srv]"
            specific = None
            if len(extras) > 0:
                specific = extras[0]
            logging.info("Got status command, specific: %s", specific)

            status = create_status(specific)
            logging.info("Sending reply: %s", status)
            client.publish(topic_report_conf, json.dumps(status), qos=1)

        case "logs":
            # TODO send program logs and error logs
            # Extra options: "/(mqtt|speedtest)/[n]"
            # n: read last n lines, default 20
            logging.info("Got logs command.")
            n_lines = 20
            if len(extras) > 0:
                target = extras[0]
                if target not in logpaths:
                    logging.error("Invalid target! %s", target)
                    msg = create_msg("logs", {"returncode": 1},
                                     "Invalid target!")
                    logging.info("Sending reply: %s", msg)
                    client.publish(topic_report_conf, json.dumps(msg), qos=1)
                    return
                if len(extras) > 1:
                    try:
                        n_lines = int(extras[1])
                    except Exception:
                        logging.error("Invalid number of lines! %s", extras[1])
                        msg = create_msg("logs", {"returncode": 1},
                                         "Invalid number of lines!")
                        logging.info("Sending reply: %s", msg)
                        client.publish(topic_report_conf, json.dumps(msg),
                                       qos=1)
                        return
            else:
                logging.error("Must specify target!")
                msg = create_msg("logs", {"returncode": 1},
                                 "Must specify target!")
                logging.info("Sending reply: %s", msg)
                client.publish(topic_report_conf, json.dumps(msg), qos=1)
                return

            logging.info("Target: %s, n: %s", target, n_lines)
            output = utils.run_cmd(
                (f"tail -n {n_lines} {logpaths[target]}"),
                raw_out=True)
            msg = create_msg(f"logs/{target}",
                             {"returncode": output["returncode"],
                              "log": output["stdout"]},
                             ("" if output["returncode"] == 0
                              else output["stderr"]))
            logging.info("Sending reply: %s", msg)
            client.publish(topic_report_conf, json.dumps(msg), qos=1)

        case "gitreset":
            # Restart services
            # Extra options: "/branch_name"
            logging.info("Got gitreset command.")
            if len(extras) > 0:
                branch_name = extras[0]
                if branch_name not in ["main", "testing", "experimental"]:
                    logging.error("Invalid branch name! %s", branch_name)
                    msg = create_msg("gitreset", {"returncode": 1},
                                     "Invalid branch name!")
                    logging.info("Sending reply: %s", msg)
                    client.publish(topic_report_conf, json.dumps(msg), qos=1)
                    return
            else:
                logging.error("Must specify branch name!")
                msg = create_msg("gitreset", {"returncode": 1},
                                 "Must specify branch name!")
                logging.info("Sending reply: %s", msg)
                client.publish(topic_report_conf, json.dumps(msg), qos=1)
                return

            logging.info("Branch name: %s", branch_name)
            output = utils.run_cmd(
                (f"git fetch && git reset --hard origin/{branch_name}"),
                raw_out=True)
            msg = create_msg(f"gitreset/{branch_name}",
                             {"returncode": output["returncode"],
                              "stdout": output["stdout"]},
                             ("" if output["returncode"] == 0
                              else output["stderr"]))
            logging.info("Sending reply: %s", msg)
            client.publish(topic_report_conf, json.dumps(msg), qos=1)

        case "restartsrv":
            # Restart services
            # Extra options: "/[mqtt|speedtest]"
            target = "all"
            if len(extras) > 0:
                target = extras[0]
                if target not in logpaths:
                    logging.error("Invalid target! %s", target)
                    msg = create_msg("restartsrv", {"returncode": 1},
                                     "Invalid target!")
                    logging.info("Sending reply: %s", msg)
                    client.publish(topic_report_conf, json.dumps(msg), qos=1)
                    return
            logging.info("Got restartsrv command, target: %s", target)

            outdict = dict()
            errdict = dict()
            if (target == "all") or (target == "speedtest"):
                logging.info("Restarting speedtest...")
                output = utils.run_cmd(
                    "sudo systemctl restart speedtest_logger.service",
                    raw_out=True)
                logging.debug(output)
                outdict["speedtest"] = output["returncode"]
                if output["returncode"] != 0:
                    errdict["speedtest"] = output["stderr"]
            if (target == "all") or (target == "mqtt"):
                logging.info("Restarting mqtt...")
                # Write down last command, assuming successful update
                outdict["mqtt"] = 0
                msg = create_msg("restartsrv", {"returncode": outdict},
                                 "" if len(errdict.keys()) == 0 else errdict)
                write_last_cmd(msg)

                # Actually run the command
                output = utils.run_cmd(
                    "sudo systemctl restart mqtt.service",
                    raw_out=True)
                # This is not reached if command succeeded.
                logging.debug(output)
                # Delete last cmd and write the actual returncode.
                delete_last_cmd()
                outdict["mqtt"] = output["returncode"]
                if outdict["returncode"] != 0:
                    errdict["mqtt"] = output["stderr"]

            # If target is not mqtt, or mqtt restart error
            msg = create_msg("restartsrv", {"returncode": outdict},
                             "" if len(errdict.keys()) == 0 else errdict)
            logging.info("Sending reply: %s", msg)
            client.publish(topic_report_conf, json.dumps(msg), qos=1)

        case "disablesrv":
            # Disable service, only speedtest_logger can be disabled
            logging.info("Got disablesrv command, disabling "
                         "speedtest_logger...")

            output = utils.run_cmd(
                "sudo systemctl stop speedtest_logger.service",
                raw_out=True)
            logging.debug(output)
            if (output["returncode"] == 0):
                output = utils.run_cmd(
                    "sudo systemctl disable speedtest_logger.service",
                    raw_out=True)
                logging.debug(output)
            else:
                output["stderr"] = ("Error when stopping speedtest_logger: "
                                    f"{output['stderr']}")
            if (output["returncode"] == 0):
                output = utils.run_cmd("crontab -r", raw_out=True)
                logging.debug(output)
            else:
                output["stderr"] = ("Error when disabling speedtest_logger: "
                                    f"{output['stderr']}")
            msg = create_msg("disablesrv",
                             {"returncode": output["returncode"]},
                             output["stderr"])
            logging.info("Sending reply: %s", msg)
            client.publish(topic_report_conf, json.dumps(msg), qos=1)

        case "reboot":
            # Reboot Pi
            logging.info("Got reboot command")
            # Write down last command, assuming successful update
            msg = create_msg("reboot", {"returncode": 0})
            write_last_cmd(msg)

            output = utils.run_cmd("sudo reboot", raw_out=True)
            # The reboot command is not blocking regardless the results,
            # so we only reply if the cmd throws error.
            logging.debug(output)
            if output["returncode"] != 0:
                delete_last_cmd()
                msg = create_msg("reboot",
                                 {"returncode": output["returncode"]},
                                 output["stderr"])
                logging.info("Sending reply: %s", msg)
                client.publish(topic_report_conf, json.dumps(msg), qos=1)

        case _:
            logging.warning("Unknown command: %s", command)


def publish_msg(client):
    report = create_status()
    logging.info("Publishing report: %s", report)
    client.publish(topic_report, json.dumps(report), qos=1, retain=True)


def load_mqtt_auth():
    auth_path = Path(".mqtt-config.json")
    timeout_s = 60
    while not auth_path.is_file():
        logging.warning(("mqtt-config not found! waiting to be downloaded by "
                         "speedtest_logger, sleeping for %d s"), timeout_s)
        time.sleep(timeout_s)

    with open(auth_path, "r") as file:
        return json.load(file)


def main():
    client = mqtt.Client(
        client_id=mac,
        callback_api_version=mqtt.CallbackAPIVersion.VERSION1)
    client.on_connect = on_connect
    client.on_message = on_message
    # This is needed because when the thread throws exception, the main process
    # will still running and systemctl will not restart the script.
    client.suppress_exceptions = True

    auth = load_mqtt_auth()
    client.username_pw_set(auth['username'], auth['password'])
    client.connect(config['broker_addr'], int(config['broker_port']), 60)
    client.loop_start()
    restore_last_cmd(client)

    try:
        while True:
            publish_msg(client)
            logging.info("Sleeping for {}s, waking up at {}".format(
                config["publish_interval"],
                (datetime.now(timezone.utc).astimezone() + timedelta(
                    0, config["publish_interval"])).isoformat()))
            time.sleep(config["publish_interval"])
    except KeyboardInterrupt:
        logging.info("Disconnecting from the broker...")
        client.disconnect()


if __name__ == '__main__':
    main()
