import re
import utils

re_sub = re.compile(r"\s+")

re_patterns = {
    "bssid": re.compile(r"Address: *([0-9A-F:]+)"),
    "channel": re.compile(r"Channel: *(\d+)"),
    "freq": re.compile(r"Frequency: *([\d\.]+ ?.Hz)"),
    "rssi": re.compile(r"Signal level= *([-\d\.]+ ?dBm)"),
    "ssid": re.compile(r"ESSID: *\"([^\"]+)\""),
    "rates": re.compile(r"\d+ Mb/s"),
    "extras": re.compile(r"IE: +Unknown: +([0-9A-F]+)")
}

re_tx_bitrate = re.compile(r"tx bitrate: *(.+)")
re_rx_bitrate = re.compile(r"rx bitrate: *(.+)")
re_timestamp = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2},\d+[+-]\d{2}:\d{2}")
re_link_rssi = re.compile(r"signal: *([-\d\.]+ ?dBm)")


def byte_uint_to_int(byte_uint):
    out = byte_uint
    # check sign bit
    if (out & 0x8000) == 0x8000:
        # if set, invert and add one to get the negative value,
        # then add the negative sign
        out = -((out ^ 0xffff) + 1)
    return out


def read_beacon_ie(ie_hex_string):
    output = {
        "id": 0,
        "raw": ie_hex_string,
        "type": "",
        "elements": {}
    }
    # Convert hex string to bytes
    byte_data = bytes.fromhex(ie_hex_string)
    output["id"] = byte_data[0]

    match output["id"]:
        case 11:
            # BSS Load
            output["type"] = "BSS Load"
            output["elements"]["sta_count"] = int.from_bytes(
                byte_data[2:4], byteorder='little')
            output["elements"]["ch_utilization"] = byte_data[4] / 255
            output["elements"]["available_admission_cap"] = int.from_bytes(
                byte_data[5:7], byteorder='little')
        case 35:
            # TPC Report
            output["type"] = "TPC Report"
            output["elements"]["tx_power"] = byte_uint_to_int(byte_data[2])
            output["elements"]["link_margin"] = byte_uint_to_int(byte_data[3])
        case 61:
            # HT Operation
            output["type"] = "HT Operation"
            output["elements"]["primary_channel"] = byte_data[2]

            ht_operation_info = byte_data[3:8]
            output["elements"]["secondary_channel_offset"] = (
                ht_operation_info[0] & 0x03)
            output["elements"]["sta_channel_width"] = (
                (ht_operation_info[0] >> 2) & 0x01)
            output["elements"]["rifs_mode"] = (
                (ht_operation_info[0] >> 3) & 0x01)
            output["elements"]["ht_protection"] = (
                ht_operation_info[1] & 0x02)
            output["elements"]["nongf_ht_sta_present"] = (
                (ht_operation_info[1] >> 2) & 0x01)
            output["elements"]["obss_nonht_sta_present"] = (
                (ht_operation_info[1] >> 4) & 0x01)
            output["elements"]["channel_center_freq_segment_2"] = (
                ((ht_operation_info[1] >> 5) & 0x03)
                + (ht_operation_info[2] << 8))
            output["elements"]["dual_beacon"] = (
                (ht_operation_info[3] >> 6) & 0x01)
            output["elements"]["dual_cts_protection"] = (
                (ht_operation_info[3] >> 7) & 0x01)
            output["elements"]["stbc_beacon"] = (
                ht_operation_info[4] & 0x01)
            output["elements"]["lsig_txop_protection"] = (
                (ht_operation_info[4] >> 1) & 0x01)
            output["elements"]["pco_active"] = (
                (ht_operation_info[4] >> 2) & 0x01)
            output["elements"]["pco_phase"] = (
                (ht_operation_info[4] >> 3) & 0x01)

            output["elements"]["basic_mcs_set"] = int.from_bytes(
                byte_data[8:24], byteorder='little')
        case 192:
            # VHT Operation
            output["type"] = "VHT Operation"
            output["elements"]["channel_width"] = byte_data[2]
            output["elements"]["channel_center_freq_0"] = byte_data[3]
            output["elements"]["channel_center_freq_1"] = byte_data[4]
            output["elements"]["basic_mcs_set"] = int.from_bytes(
                byte_data[5:7], byteorder='little')
        case 255:
            output["elements"]["ext_id"] = byte_data[2]
            match output["elements"]["ext_id"]:
                case 36:
                    # HE Operation
                    output["type"] = "HE Operation"
                    he_operation_info = byte_data[3:6]
                    output["elements"]["default_pe_duration"] = (
                        he_operation_info[0] & 0x07)
                    output["elements"]["twt_required"] = (
                        (he_operation_info[0] >> 3) & 0x01)
                    output["elements"]["txop_dur_rts_thresh"] = (
                        (he_operation_info[0] >> 4) & 0x0F
                        + (he_operation_info[1] & 0x3F))
                    output["elements"]["vht_info_present"] = (
                        (he_operation_info[1] >> 6) & 0x01)
                    output["elements"]["cohosted_bss"] = (
                        (he_operation_info[1] >> 7) & 0x01)
                    output["elements"]["er_su_disable"] = (
                        he_operation_info[2] & 0x01)
                    output["elements"]["6ghz_info_present"] = (
                        (he_operation_info[2] >> 1) & 0x01)

                    bss_color_info = byte_data[6]
                    output["elements"]["bss_color"] = (
                        bss_color_info & 0x3F)
                    output["elements"]["partial_bss_color"] = (
                        (bss_color_info >> 6) & 0x01)
                    output["elements"]["bss_color_disabled"] = (
                        (bss_color_info >> 7) & 0x01)

                    output["elements"]["basic_mcs_set"] = int.from_bytes(
                        byte_data[7:9], byteorder='little')

                    start_index = 9
                    if (output["elements"]["vht_info_present"]):
                        # Decode VHT info
                        output["elements"]["vht_info"] = {
                            "channel_width": byte_data[start_index],
                            "channel_center_freq_0": byte_data[start_index + 1],
                            "channel_center_freq_1": byte_data[start_index + 2]
                        }
                        start_index += 3

                    if (output["elements"]["cohosted_bss"]):
                        output["elements"]["max_cohosted_bss_indicator"] = byte_data[start_index]
                        start_index += 1

                    if (output["elements"]["6ghz_info_present"]):
                        control = byte_data[start_index + 1]

                        output["elements"]["6ghz_info"] = {
                            "primary_channel": byte_data[start_index],
                            "channel_width": (control & 0x03),
                            "duplicate_beacon": ((control >> 2) & 0x01),
                            "regulatory_info": ((control >> 3) & 0x07),
                            "channel_center_freq_0": byte_data[start_index + 2],
                            "channel_center_freq_1": byte_data[start_index + 3],
                            "min_rate": byte_data[start_index + 4],
                        }
                        start_index += 5
                case _:
                    output["type"] = "Unknown"
        case _:
            output["type"] = "Unknown"

    return output


def process_link(result):
    # Get connected BSSID and bitrate
    bssid = ""
    tx_bitrate = ""
    rx_bitrate = ""
    if (result):
        re_connected = re.compile(
            r"Connected to *([\da-f]{2}:[\da-f]{2}:[\da-f]{2}:[\da-f]{2}:[\da-f]{2}:[\da-f]{2})")
        matches = re_connected.findall(result)
        if (len(matches) > 0):
            bssid = matches[0].upper()
        matches = re_tx_bitrate.findall(result)
        if (len(matches) > 0):
            tx_bitrate = matches[0]
        matches = re_rx_bitrate.findall(result)
        if (len(matches) > 0):
            rx_bitrate = matches[0]

    return {
        "bssid": bssid,
        "tx_bitrate": tx_bitrate,
        "rx_bitrate": rx_bitrate,
    }


def process_link_results(results):
    # Get timestamps and bitrates
    timestamps = re_timestamp.findall(results)
    rssis = re_link_rssi.findall(results)
    tx_bitrates = re_tx_bitrate.findall(results)
    rx_bitrates = re_rx_bitrate.findall(results)

    out_arr = []
    for i in range(0, len(rx_bitrates)):
        out_arr.append({
            "timestamp": timestamps[i].replace(",", "."),
            "rssi": rssis[i],
            "tx_bitrate": tx_bitrates[i],
            "rx_bitrate": rx_bitrates[i]
        })

    return out_arr


def process_scan_results(results, wifi_link):
    # Process Wi-Fi scan results
    results = re_sub.sub(" ", results).split("Cell")
    cells = []
    for entry in results:
        cell = {
            "bssid": "",
            "channel": "",
            "freq": "",
            "rssi": "",
            "ssid": "",
            "connected": False,
            "rates": [],
            "tx_bitrate": "",
            "rx_bitrate": "",
            "extras": []
        }

        for key in re_patterns:
            matches = re_patterns[key].findall(entry)
            if (len(matches) > 0):
                if key == "extras":
                    for ie_hex in matches:
                        # Convert hex string to information element dict
                        ie = read_beacon_ie(ie_hex)
                        cell[key].append(ie)
                elif key == "rates":
                    cell[key] = matches
                else:
                    cell[key] = matches[0]

        if cell["bssid"] != "":
            if cell["bssid"] == wifi_link["bssid"]:
                cell["connected"] = True
                cell["tx_bitrate"] = wifi_link["tx_bitrate"]
                cell["rx_bitrate"] = wifi_link["rx_bitrate"]
            cells.append(cell)

    return cells


def scan(iface="wlan0"):
    # Scan Wi-Fi beacons
    results = utils.run_cmd(
        "sudo iwlist {} scanning".format(iface),
        "Scanning Wi-Fi beacons",
        log_result=False)

    # Get Wi-Fi link
    result_conn = utils.run_cmd(
        "sudo iw dev {} link".format(iface),
        "Get connected Wi-Fi")

    return process_scan_results(results, process_link(result_conn))


def scan_async(iface, link_wait):
    # Start scanning asynchronously
    # Run "iw dev link" after sleeping to capture connected state at perf test
    return {
        "scan": utils.run_cmd_async(
            "sudo iwlist {} scanning".format(iface),
            "Scanning Wi-Fi beacons asynchronously"),
        "link": utils.run_cmd_async(
            "sleep {}; sudo iw dev {} link".format(link_wait, iface),
            "Get connected Wi-Fi link")
    }


def resolve_scan_async(proc_obj):
    results = utils.resolve_cmd_async(
        proc_obj["scan"],
        "Resolving Wi-Fi beacon scan",
        log_result=False)
    result_conn = utils.resolve_cmd_async(
        proc_obj["link"],
        "Resolving Wi-Fi link",
        log_result=False)

    return process_scan_results(results, process_link(result_conn))


def link_async(iface):
    # Continuouly run "iw dev link" to capture Wi-Fi link's bitrates
    return utils.run_cmd_async(
        "while true; do date -Ins; sudo iw dev {} link; done".format(iface),
        "Continuouly get Wi-Fi link")


def resolve_link_async(proc):
    results = utils.resolve_cmd_async(
        proc,
        "Resolving repeated Wi-Fi link call",
        log_result=False,
        kill=True)

    return process_link_results(results)


if __name__ == '__main__':
    print(scan())
