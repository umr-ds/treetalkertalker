import logging
import time

from typing import Union, Dict, List
from collections import defaultdict
from statistics import mean, stdev

from dataclasses import dataclass

import influxdb as influx
from influxdb.resultset import ResultSet
from sklearn.linear_model import LinearRegression

from ttt.packets import (
    DataPacketRev31,
    DataPacketRev32,
    LightSensorPacket,
    TTCommand1,
    TTCommand2,
    TTAddress,
)
from ttt.util import (
    compute_temperature,
    compute_battery_voltage_rev_3_1,
    compute_battery_voltage_rev_3_2,
)


RDE = 1
ANALYSIS_INTERVAL = "2d"
SLEEP_TIME_MIN = 60


@dataclass
class DataPolicy:
    local_address: TTAddress
    influx_client: influx.InfluxDBClient
    aggregated_movement: Dict[str, float]
    aggregated_temperature: Dict[str, float]

    def _evaluate_battery_3_2(self, packet: DataPacketRev32) -> int:
        battery_voltage = compute_battery_voltage_rev_3_2(
            adc_volt_bat=packet.adc_volt_bat, adc_bandgap=packet.adc_bandgap
        )
        return self._evaluate_battery(
            sender_address=packet.sender_address.address,
            battery_voltage=battery_voltage,
        )

    def _evaluate_battery_3_1(self, packet: DataPacketRev31) -> int:
        battery_voltage = compute_battery_voltage_rev_3_1(voltage=packet.voltage)
        return self._evaluate_battery(
            sender_address=packet.sender_address.address,
            battery_voltage=battery_voltage,
        )

    def _evaluate_battery(self, sender_address: int, battery_voltage: float) -> int:
        data: ResultSet = self.influx_client.query(
            f'SELECT "ttt_voltage" FROM "power" WHERE time > now() - {ANALYSIS_INTERVAL} AND treealker = {sender_address}'
        )
        times = []
        voltages = []
        for datapoint in data.get_points("power"):
            timestamp = int(
                time.mktime(time.strptime(datapoint["time"], "%Y-%m-%dT%H:%M:%S.%fZ"))
            )
            times.append([timestamp])
            voltages.append(datapoint["ttt_voltage"])

        times.append([int(time.time())])
        voltages.append(battery_voltage)

        reg: LinearRegression = LinearRegression().fit(times, voltages)

        try:
            measurement_interval = next(
                self.influx_client.query(
                    f'SELECT last("measurement_interval") FROM "measurement_interval" WHERE treealker = {sender_address}'
                ).get_points("power")
            )[
                "last"
            ]  # I hate this monstrosity and I hate influx for making me do this...
        except StopIteration:
            measurement_interval = 3600

        measurement_interval = int(
            measurement_interval
            + (RDE * (3700 - reg.predict([[int(time.time()) + (3600 * 48)]])[0]))
        )

        influx_data = [
            {
                "measurement": "measurement_interval",
                "tags": {
                    "treetalker": sender_address,
                },
                "fields": {
                    "measurement_interval": measurement_interval,
                },
            },
        ]
        self.influx_client.write_points(influx_data)

        return measurement_interval

    def _evaluate_position(
        self, packet: DataPacketRev32, means: Dict[str, List[int]]
    ) -> bool:
        mean_x = mean(means["x"])
        stdev_x = stdev(means["x"])
        mean_y = mean(means["y"])
        stdev_y = stdev(means["y"])
        mean_z = mean(means["z"])
        stdev_z = stdev(means["z"])

        x = packet.gravity_x_mean
        y = packet.gravity_y_mean
        z = packet.gravity_z_mean

        return (
            abs(x - mean_x) > stdev_x
            or abs(y - mean_y) > stdev_y
            or abs(z - mean_z) > stdev_z
        )

    def _evaluate_movement(self, packet: DataPacketRev32) -> bool:
        if not self.aggregated_movement:
            logging.info("Haven't received any aggregated movement data yet.")
            return False

        x = packet.gravity_x_derivation
        y = packet.gravity_y_derivation
        z = packet.gravity_z_derivation

        return (
            abs(x - self.aggregated_movement["mean_x"])
            > self.aggregated_movement["stdev_x"]
            or abs(y - self.aggregated_movement["mean_y"])
            > self.aggregated_movement["stdev_y"]
            or abs(z - self.aggregated_movement["mean_z"])
            > self.aggregated_movement["stdev_z"]
        )

    def _evaluate_gravity(self, packet: Union[DataPacketRev31, DataPacketRev32]) -> int:
        means: Dict[str, List[int]] = defaultdict(list)
        data: ResultSet = self.influx_client.query(
            f'SELECT "x_mean", "y_mean", "z_mean" FROM "gravity" WHERE time > now() - {ANALYSIS_INTERVAL} AND treealker = {packet.sender_address.address}'
        )

        for datapoint in data.get_points("gravity"):
            means["x"].append(datapoint["x_mean"])
            means["y"].append(datapoint["y_mean"])
            means["z"].append(datapoint["z_mean"])

        return self._evaluate_position(
            packet=packet, means=means
        ) or self._evaluate_movement(packet=packet)

    def _evaluate_temperature(
        self, packet: Union[DataPacketRev31, DataPacketRev32]
    ) -> bool:
        if not self.aggregated_movement:
            logging.info("Haven't received any aggregated temperature data yet.")
            return False

        temperature_reference_cold = compute_temperature(
            packet.temperature_reference[0]
        )
        temperature_reference_hot = compute_temperature(packet.temperature_reference[1])
        temperature_heat_cold = compute_temperature(packet.temperature_heat[0])
        temperature_heat_hot = compute_temperature(packet.temperature_heat[1])
        delta_cold = abs(temperature_heat_cold - temperature_reference_cold)
        delta_hot = abs(temperature_heat_hot - temperature_reference_hot)

        data: ResultSet = self.influx_client.query(
            f'SELECT "ttt_reference_probe_cold","ttt_reference_probe_hot","ttt_heat_probe_cold","ttt_heat_probe_hot" FROM "stem_temperature" WHERE time > now() - {ANALYSIS_INTERVAL} AND treealker = {packet.sender_address.address}'
        )

        reference_probe_cold: List[float] = []
        reference_probe_hot: List[float] = []
        heat_probe_cold: List[float] = []
        heat_probe_hot: List[float] = []

        for datapoint in data.get_points("stem_temperature"):
            reference_probe_cold.append(datapoint["ttt_reference_probe_cold"])
            reference_probe_hot.append(datapoint["ttt_reference_probe_hot"])
            heat_probe_cold.append(datapoint["ttt_heat_probe_cold"])
            heat_probe_hot.append(datapoint["ttt_heat_probe_hot"])

        deltas_cold: List[float] = [
            abs(heat - reference)
            for heat, reference in zip(heat_probe_cold, reference_probe_cold)
        ]
        mean_delta_cold = mean(deltas_cold)

        deltas_hot: List[float] = [
            abs(heat - reference)
            for heat, reference in zip(heat_probe_hot, reference_probe_hot)
        ]
        mean_delta_hot = mean(deltas_hot)

        return (
            abs(delta_cold - mean_delta_cold)
            > self.aggregated_temperature["stdev_delta_cold"]
            or abs(delta_hot - mean_delta_hot)
            > self.aggregated_temperature["stdev_delta_hot"]
        )

    def evaluate_3_2(self, packet: DataPacketRev32) -> TTCommand1:
        sleep_interval: int = max(
            self._evaluate_battery_3_2(packet=packet), SLEEP_TIME_MIN
        )

        if self._evaluate_gravity(packet=packet) or self._evaluate_temperature(
            packet=packet
        ):
            sleep_interval = SLEEP_TIME_MIN

        heating = int(sleep_interval / 6)

        return TTCommand1(
            receiver_address=packet.sender_address,
            sender_address=self.local_address,
            command=32,
            time=int(time.time()),
            sleep_interval=sleep_interval,
            unknown=(0, 45, 1),
            heating=heating,
        )

    def evaluate_3_1(self, packet: DataPacketRev31) -> TTCommand1:
        sleep_interval: int = max(
            self._evaluate_battery_3_1(packet=packet), SLEEP_TIME_MIN
        )

        if self._evaluate_gravity(packet=packet) or self._evaluate_temperature(
            packet=packet
        ):
            sleep_interval = SLEEP_TIME_MIN

        heating = int(sleep_interval / 6)

        return TTCommand1(
            receiver_address=packet.sender_address,
            sender_address=self.local_address,
            command=32,
            time=int(time.time()),
            sleep_interval=sleep_interval,
            unknown=(0, 45, 1),
            heating=heating,
        )


@dataclass
class LightPolicy:
    local_address: TTAddress
    influx_client: influx.InfluxDBClient

    def _evaluate_brightness(self, packet: LightSensorPacket) -> int:
        # Welche Variable enthält dies?
        pass

    def evaluate(self, packet: LightSensorPacket) -> TTCommand2:
        return TTCommand2(
            receiver_address=packet.sender_address,
            sender_address=self.local_address,
            command=33,
            time=int(time.time()),
            integration_time=50,
            gain=3,
        )
