from datetime import datetime
import jc
import logging
import re
import utils


def get_gateway_ip(iface):
    logging.info(f"Fetching gateway IP of {iface}.")
    re_gateway = re.compile(rf"default via (\d+\.\d+\.\d+\.\d+) dev {iface}")

    result = utils.run_cmd(
        "ip route",
        "Fetching gateway IP")

    re_results = re_gateway.findall(result)
    if (len(re_results) > 0):
        return re_results[0]
    else:
        return ""


def process_ping_results(results):
    parsed = jc.parse("ping", results)
    for entry in parsed["responses"]:
        if "timestamp" in entry:
            entry["timestamp"] = datetime.fromtimestamp(
                entry["timestamp"]).astimezone().isoformat()

    return parsed


def ping(iface, ping_target, ping_count):
    gateway = get_gateway_ip(iface)
    if (not gateway):
        logging.warning("Cannot find gateway!")
        return

    logging.info("Running ping to target %s and gateway %s.",
                 ping_target, gateway)

    output = list()
    results = utils.run_cmd(
        f"ping {ping_target} -Dc {ping_count}",
        f"Running ping to {ping_target}",
        log_result=False)
    output.append(process_ping_results(results))
    results = utils.run_cmd(
        f"ping {gateway} -Dc {ping_count}",
        f"Running ping to {gateway}",
        log_result=False)
    output.append(process_ping_results(results))

    return output


def ping_async(iface, ping_target):
    gateway = get_gateway_ip(iface)
    if (not gateway):
        logging.warning("Cannot find gateway!")
        return

    logging.info("Running asynchronous ping to target %s and gateway %s.",
                 ping_target, gateway)
    return {
        "target": utils.run_cmd_async(
            f"ping {ping_target} -D",
            f"Running ping to {ping_target}"),
        "gateway": utils.run_cmd_async(
            f"ping {gateway} -D",
            f"Running ping to {gateway}")}


def resolve_ping_async(proc_obj):
    logging.info("Resolving ping.")
    output = list()

    results = utils.resolve_cmd_async(
        proc_obj["target"],
        "Resolving ping to target",
        log_result=False,
        kill=True)
    output.append(process_ping_results(results))

    results = utils.resolve_cmd_async(
        proc_obj["gateway"],
        "Resolving ping to gateway",
        log_result=False,
        kill=True)
    output.append(process_ping_results(results))

    return output
