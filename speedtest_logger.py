from datetime import datetime, timedelta, timezone
import firebase
from getpass import getuser
import json
import logging
from logging.handlers import TimedRotatingFileHandler
from logging import Formatter
import ping
from random import randint, uniform
import time
import utils
from uuid import uuid4
import wifi_scan

logdir = "/home/{}/airbuddy/logs".format(getuser())

# Logging setup
handler = TimedRotatingFileHandler(
    filename="{}/speedtest_logger.log".format(logdir),
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


def set_interface_down(iface, conn=False):
    logging.info("Setting interface %s down.", iface)
    if (conn):
        utils.run_cmd("sudo nmcli connection down '{}'".format(conn),
                      "Set connection {} down".format(conn))
    utils.run_cmd("sudo ip link set {} down".format(iface),
                  "Set interface {} link down".format(iface))


def set_interface_up(iface, conn=False):
    logging.info("Setting interface %s up.", iface)
    utils.run_cmd("sudo ip link set {} up".format(iface),
                  "Set interface {} link up".format(iface))
    if (conn):
        retry_count = 0
        retry_max = 10
        while retry_count < retry_max:
            output = utils.run_cmd("sudo nmcli connection up '{}'".format(conn),
                                   "Set connection {} up".format(conn))
            if (output.find("successfully") >= 0):
                retry_count += retry_max
            else:
                retry_count += 1
                time.sleep(1)
                logging.debug("Error setting conn up, retry count: %s",
                              retry_count)


def enable_monitor(iface):
    logging.info("Enabling interface %s as monitor.", iface)
    is_monitor = "monitor" in (
        utils.run_cmd("sudo iw dev {} info".format(iface),
                      "Checking iface {} info".format(iface)))
    logging.info("{} is monitor? {}".format(iface, is_monitor))
    if (not is_monitor):
        set_interface_down(iface)
        utils.run_cmd("sudo iw dev {} set type monitor".format(iface),
                      "Set interface {} as monitor".format(iface))
    set_interface_up(iface)


def disable_monitor(iface):
    logging.info("Disabling interface %s as monitor.", iface)
    is_monitor = "monitor" in (
        utils.run_cmd("sudo iw dev {} info".format(iface),
                      "Checking iface {} info".format(iface)))
    logging.info("{} is monitor? {}".format(iface, is_monitor))
    if (is_monitor):
        set_interface_down(iface)
        utils.run_cmd("sudo iw dev {} set type managed".format(iface),
                      "Set interface {} as managed".format(iface))
    set_interface_up(iface)


def setup_network(wifi_conn, wireless_iface, monitor_iface):
    logging.info("Setting up network.")

    # Set all interface link up, just in case
    set_interface_up("eth0")
    set_interface_up(wireless_iface)
    disable_monitor(wireless_iface)
    if (monitor_iface and wireless_iface != monitor_iface):
        enable_monitor(monitor_iface)

    # Check available eth and wlan connection in nmcli
    conn_found = False
    result = utils.run_cmd("sudo nmcli --terse connection show",
                           "Checking available connections")
    for line in result.splitlines():
        split = line.split(":")
        if (split[2] == "802-11-wireless" and wifi_conn):
            # Delete the connection if wifi_conn available from Firebase
            # and the current connection is not wifi_conn
            if (wifi_conn["ssid"] != split[0]):
                # Delete the possibly unused connection
                utils.run_cmd(("sudo nmcli connection "
                               "delete '{}'").format(split[0]),
                              "Deleting wlan connection {}".format(split[0]))
            else:
                # Otherwise the connection is found
                conn_found = True
        elif (split[2] == "802-3-ethernet"):
            # If the connection is ethernet, try to connect
            set_interface_up("eth0", split[0])

    # Try connect Wi-Fi using info from Firebase
    if (not conn_found and wifi_conn):
        result = utils.run_cmd(
            "sudo nmcli device wifi connect '{}' password '{}'".format(
                wifi_conn["ssid"], wifi_conn["pass"]),
            "Adding SSID '{}'".format(wifi_conn["ssid"]))
        conn_found = (result.find("successfully") >= 0)

    if (conn_found):
        # Check if connection need editing.
        conn_iface = utils.run_cmd(
            ("sudo nmcli --fields connection.interface-name connection "
             "show '{}'").format(wifi_conn["ssid"]),
            "Check connection {} interface".format(wifi_conn["ssid"]))
        edit_iface = wireless_iface not in conn_iface
        logging.debug("Edit connection %s interface? %s",
                      wifi_conn["ssid"], edit_iface)
        edit_bssid = False
        if ("bssid" in wifi_conn):
            conn_iface = utils.run_cmd(
                ("sudo nmcli --fields 802-11-wireless.bssid connection "
                 "show '{}'").format(wifi_conn["ssid"]),
                "Check connection {} interface".format(wifi_conn["ssid"]))
            if (wifi_conn["bssid"] == ""):
                edit_bssid = "--" not in conn_iface
            else:
                edit_bssid = wifi_conn["bssid"] not in conn_iface
            logging.debug("Edit connection %s BSSID? %s",
                          wifi_conn["ssid"], edit_bssid)

        if (edit_bssid or edit_iface):
            # Put new connection down temporarily for editing
            utils.run_cmd(("sudo nmcli connection "
                           "down '{}'").format(wifi_conn["ssid"]),
                          ("Setting connection {} "
                           "down temporarily").format(wifi_conn["ssid"]))
            # Ensure that the connection is active on selected iface
            if (edit_iface):
                utils.run_cmd(
                    ("sudo nmcli connection modify '{}' "
                     "connection.interface-name '{}'").format(
                        wifi_conn["ssid"], wireless_iface),
                    "Setting connection '{}' to '{}'".format(
                        wifi_conn["ssid"], wireless_iface))
            # If BSSID is in connection info, add it
            if (edit_bssid):
                utils.run_cmd(
                    ("sudo nmcli connection modify '{}' "
                     "802-11-wireless.bssid '{}'").format(
                        wifi_conn["ssid"], wifi_conn["bssid"]),
                    "Setting connection '{}' BSSID to '{}'".format(
                        wifi_conn["ssid"], wifi_conn["bssid"]))

        # Activate connection, should run whether the connection is up or down
        utils.run_cmd(("sudo nmcli connection "
                       "up '{}'").format(wifi_conn["ssid"]),
                      ("Setting connection {} "
                       "up").format(wifi_conn["ssid"]))

    # Check all interfaces status
    result = utils.run_cmd("sudo nmcli --terse device status",
                           "Checking network interfaces status")
    eth_connection = False
    wifi_connection = False
    for line in result.splitlines():
        split = line.split(":")
        if (split[0] == "eth0"):
            eth_connection = split[3]
        elif (split[0] == wireless_iface):
            wifi_connection = split[3]
    logging.debug("eth0 connection: %s.", eth_connection)
    logging.debug("%s connection: %s.", wireless_iface, wifi_connection)

    return {"eth": eth_connection, "wifi": wifi_connection}


def run_iperf(test_uuid, server, port, direction, duration, dev, timeout_s):
    # Run iperf command
    iperf_cmd = ("iperf3 -c {} -p {} -t {} -P 8 -b 2000M -J").format(
        server, port, duration)
    if (direction == "dl"):
        iperf_cmd += " -R"
    result = utils.run_cmd(
        iperf_cmd,
        "Running iperf command",
        log_result=False,
        timeout_s=timeout_s)

    if (result):
        result_json = json.loads(result)
        result_json["start"]["interface"] = dev
        result_json["start"]["test_uuid"] = test_uuid

        # Log this data
        with open("logs/iperf-log/{}.json".format(
            datetime.now(timezone.utc).astimezone().isoformat()
        ), "w") as log_file:
            log_file.write(json.dumps(result_json))

        if direction == "dl":
            data_used = result_json["end"]["sum_received"]["bytes"] / 1e9
        else:
            data_used = result_json["end"]["sum_sent"]["bytes"] / 1e9
        logging.info("Data used for iperf %.3f GB", data_used)
        return data_used
    else:
        return 0


def run_speedtest(test_uuid, timeout_s):
    # Run the speedtest command
    result = utils.run_cmd(
        "./speedtest --accept-license --format=json",
        "Running speedtest command",
        log_result=False,
        timeout_s=timeout_s)

    if (result):
        result_json = json.loads(result)
        result_json["test_uuid"] = test_uuid

        # Log this data
        with open("logs/speedtest-log/{}.json".format(
            datetime.now(timezone.utc).astimezone().isoformat()
        ), "w") as log_file:
            log_file.write(json.dumps(result_json))

        data_used = (result_json["download"]["bytes"]
                     + result_json["upload"]["bytes"]) / 1e9
        logging.info("Data used for Ookla %.3f GB", data_used)
        return data_used
    else:
        return 0


def scan_wifi(iface, extra):
    # Run Wi-Fi scan
    logging.info("Starting Wi-Fi scan.")
    results = wifi_scan.scan(iface)
    timestamp = datetime.now(timezone.utc).astimezone().isoformat()

    # Log this data
    with open("logs/wifi-scan/{}.json".format(timestamp), "w") as log_file:
        log_file.write(
            json.dumps({
                "timestamp": timestamp,
                "interface": iface,
                "extra": extra,
                "beacons": results}))


def scan_wifi_async(iface, link_wait=1):
    # Run Wi-Fi scan
    logging.info("Starting Wi-Fi scan.")
    return {
        "proc_obj": wifi_scan.scan_async(iface, link_wait),
        "proc_link": wifi_scan.link_async(iface),
        "timestamp": datetime.now(timezone.utc).astimezone().isoformat(),
        "iface": iface
    }


def resolve_scan_wifi_async(resolve_obj, extra):
    logging.info("Resolving Wi-Fi scan.")
    results_link = wifi_scan.resolve_link_async(resolve_obj["proc_link"])
    results = wifi_scan.resolve_scan_async(resolve_obj["proc_obj"])

    # Log this data
    with open("logs/wifi-scan/{}.json".format(
              resolve_obj["timestamp"]), "w") as log_file:
        log_file.write(
            json.dumps({
                "timestamp": resolve_obj["timestamp"],
                "interface": resolve_obj["iface"],
                "extra": extra,
                "beacons": results,
                "links": results_link}))


def run_ping(iface, extra, ping_target, ping_count):
    # Run Wi-Fi scan
    logging.info("Starting ping.")
    results = ping.ping(iface, ping_target, ping_count)
    timestamp = datetime.now(timezone.utc).astimezone().isoformat()

    # Log this data
    with open("logs/ping-log/{}.json".format(timestamp), "w") as log_file:
        log_file.write(
            json.dumps({
                "timestamp": timestamp,
                "interface": iface,
                "extra": extra,
                "pings": results}))


def run_ping_async(iface, ping_target):
    # Run Wi-Fi scan
    logging.info("Starting async ping.")
    proc_obj = ping.ping_async(iface, ping_target)
    return {
        "proc_obj": proc_obj,
        "timestamp": datetime.now(timezone.utc).astimezone().isoformat(),
        "iface": iface
    }


def resolve_run_ping_async(resolve_obj, extra):
    logging.info("Resolving async ping.")
    results = ping.resolve_ping_async(resolve_obj["proc_obj"])

    # Log this data
    with open("logs/ping-log/{}.json".format(
              resolve_obj["timestamp"]), "w") as log_file:
        log_file.write(
            json.dumps({
                "timestamp": resolve_obj["timestamp"],
                "interface": resolve_obj["iface"],
                "extra": extra,
                "pings": results}))


def main():
    curr_usage_gbytes = firebase.get_data_used(mac)
    logging.info("Got lastest usage data: %.3f GB", curr_usage_gbytes)
    logging.info("Upload previously recorded logs on startup.")
    temp_used = firebase.upload_directory(
        source_dir=logdir,
        mac=mac)
    firebase.push_data_used(mac, temp_used)
    curr_usage_gbytes += temp_used

    while True:
        logging.info("Starting tests.")
        # Update config
        config = firebase.read_config(mac)
        # Random UUID to correlate WiFi scans and tests
        config["test_uuid"] = str(uuid4())
        logging.info("Config: %s", config)
        # WiFi connection
        config["wifi_conn"] = firebase.get_wifi_conn(mac)

        # Ensure Ethernet and Wi-Fi are connected
        conn_status = setup_network(
            config["wifi_conn"],
            config["wireless_interface"],
            config["monitor_interface"])
        logging.info("Connection status: %s", conn_status)

        # Send heartbeat to indicate up status
        firebase.push_heartbeat(mac)

        # If the Pi has turned on for more than 1 day, randomly pick a number
        # and check for the threshold. The default threshold is 0.25 since
        # we expect 6 daily test out of 24 hours.
        if (time.clock_gettime(time.CLOCK_BOOTTIME) < 86400
                or uniform(0, 1) < config["sampling_threshold"]):
            this_session_usage = 0

            # Start tests over ethernet
            if (conn_status["eth"]):
                logging.info("Starting tests over eth0.")
                if (conn_status["wifi"]):
                    set_interface_down(config["wireless_interface"],
                                       conn_status["wifi"])

                # Run idle ping
                run_ping(
                    "eth0",
                    extra={
                        "test_uuid": config["test_uuid"],
                        "corr_test": "idle"},
                    ping_target=config["ping_target"],
                    ping_count=config["ping_count"])

                # Disabled while FMNC is down
                # run_fmnc()

                if ((curr_usage_gbytes + this_session_usage)
                        < config["data_cap_gbytes"]):
                    # iperf downlink
                    resolve_ping_obj = run_ping_async(
                        "eth0",
                        ping_target=config["ping_target"])
                    this_session_usage += run_iperf(
                        test_uuid=config["test_uuid"],
                        server=config["iperf_server"],
                        port=randint(config["iperf_minport"],
                                     config["iperf_maxport"]),
                        direction="dl", duration=config["iperf_duration"],
                        dev="eth0", timeout_s=config["timeout_s"])
                    resolve_run_ping_async(
                        resolve_ping_obj,
                        extra={
                            "test_uuid": config["test_uuid"],
                            "corr_test": "iperf-dl"})

                if ((curr_usage_gbytes + this_session_usage)
                        < config["data_cap_gbytes"]):
                    # iperf uplink
                    resolve_ping_obj = run_ping_async(
                        "eth0",
                        ping_target=config["ping_target"])
                    this_session_usage += run_iperf(
                        test_uuid=config["test_uuid"],
                        server=config["iperf_server"],
                        port=randint(config["iperf_minport"],
                                     config["iperf_maxport"]),
                        direction="ul", duration=config["iperf_duration"],
                        dev="eth0", timeout_s=config["timeout_s"])
                    resolve_run_ping_async(
                        resolve_ping_obj,
                        extra={
                            "test_uuid": config["test_uuid"],
                            "corr_test": "iperf-ul"})

                if ((curr_usage_gbytes + this_session_usage)
                        < config["data_cap_gbytes"]):
                    # Ookla Speedtest
                    this_session_usage += run_speedtest(
                        test_uuid=config["test_uuid"],
                        timeout_s=config["timeout_s"])

                if (conn_status["wifi"]):
                    set_interface_up(config["wireless_interface"],
                                     conn_status["wifi"])

            # Start tests over Wi-Fi
            if (conn_status["wifi"]):
                logging.info("Starting tests over Wi-Fi.")
                if (conn_status["eth"]):
                    set_interface_down("eth0", conn_status["eth"])

                # Run idle ping
                resolve_scan_obj = scan_wifi_async(
                    config["wireless_interface"])
                run_ping(
                    config["wireless_interface"],
                    extra={
                        "test_uuid": config["test_uuid"],
                        "corr_test": "idle"},
                    ping_target=config["ping_target"],
                    ping_count=config["ping_count"])
                resolve_scan_wifi_async(
                    resolve_scan_obj,
                    extra={
                        "test_uuid": config["test_uuid"],
                        "corr_test": "idle"})

                # Disabled while FMNC is down
                # run_fmnc()

                if ((curr_usage_gbytes + this_session_usage)
                        < config["data_cap_gbytes"]):
                    # iperf downlink
                    resolve_ping_obj = run_ping_async(
                        config["wireless_interface"],
                        ping_target=config["ping_target"])
                    resolve_scan_obj = scan_wifi_async(
                        config["wireless_interface"])
                    this_session_usage += run_iperf(
                        test_uuid=config["test_uuid"],
                        server=config["iperf_server"],
                        port=randint(config["iperf_minport"],
                                     config["iperf_maxport"]),
                        direction="dl", duration=config["iperf_duration"],
                        dev=config["wireless_interface"],
                        timeout_s=config["timeout_s"])
                    resolve_run_ping_async(
                        resolve_ping_obj,
                        extra={
                            "test_uuid": config["test_uuid"],
                            "corr_test": "iperf-dl"})
                    resolve_scan_wifi_async(
                        resolve_scan_obj,
                        extra={
                            "test_uuid": config["test_uuid"],
                            "corr_test": "iperf-dl"})

                if ((curr_usage_gbytes + this_session_usage)
                        < config["data_cap_gbytes"]):
                    # iperf uplink
                    resolve_ping_obj = run_ping_async(
                        config["wireless_interface"],
                        ping_target=config["ping_target"])
                    resolve_scan_obj = scan_wifi_async(
                        config["wireless_interface"])
                    this_session_usage += run_iperf(
                        test_uuid=config["test_uuid"],
                        server=config["iperf_server"],
                        port=randint(config["iperf_minport"],
                                     config["iperf_maxport"]),
                        direction="ul", duration=config["iperf_duration"],
                        dev=config["wireless_interface"],
                        timeout_s=config["timeout_s"])
                    resolve_run_ping_async(
                        resolve_ping_obj,
                        extra={
                            "test_uuid": config["test_uuid"],
                            "corr_test": "iperf-ul"})
                    resolve_scan_wifi_async(
                        resolve_scan_obj,
                        extra={
                            "test_uuid": config["test_uuid"],
                            "corr_test": "iperf-ul"})

                if ((curr_usage_gbytes + this_session_usage)
                        < config["data_cap_gbytes"]):
                    # Ookla Speedtest
                    resolve_scan_obj = scan_wifi_async(
                        config["wireless_interface"])
                    this_session_usage += run_speedtest(
                        test_uuid=config["test_uuid"],
                        timeout_s=config["timeout_s"])
                    resolve_scan_wifi_async(
                        resolve_scan_obj,
                        extra={
                            "test_uuid": config["test_uuid"],
                            "corr_test": "speedtest"})

                if (conn_status["eth"]):
                    set_interface_up("eth0", conn_status["eth"])
            else:
                scan_wifi(
                    config["wireless_interface"],
                    extra={
                        "test_uuid": config["test_uuid"],
                        "corr_test": "none"})

            # Upload
            # TODO: Might run on a different interval in the future.
            this_session_usage += firebase.upload_directory(
                source_dir=logdir,
                mac=mac)
            firebase.push_data_used(mac, this_session_usage)
            curr_usage_gbytes += this_session_usage

        else:
            logging.info("Skipping test due to randomized sampling.")

        # Sleep for interval + random backoff
        interval = config["speedtest_interval"] * 60 + randint(0, 60)
        # Run heartbeat every minute if uptime is < 60 minutes
        while (time.clock_gettime(time.CLOCK_BOOTTIME) < 3600):
            logging.info("Sleeping for 60s")
            interval -= 60
            time.sleep(60)
            firebase.push_heartbeat(mac)

        # Avoid ValueError
        if (interval > 0):
            logging.info("Sleeping for {}s, waking up at {}".format(
                interval,
                (datetime.now(timezone.utc).astimezone() + timedelta(
                    0, interval)).isoformat()))
            time.sleep(interval)


if __name__ == "__main__":
    main()
