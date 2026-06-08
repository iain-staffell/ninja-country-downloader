#!/usr/bin/env python3
"""Download representative Renewables.ninja hourly capacity-factor series."""

from __future__ import annotations

import argparse
import csv
import io
import logging
import math
import os
import re
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests
from sklearn.cluster import KMeans


# you can specify the default parameters to use for all wind and solar farms here
# see https://www.renewables.ninja/api/models for a description of the parameters

WIND_PARAMETERS = {
    "capacity": 1.0,
    "height": 100,
    "turbine": "Vestas V80 2000",
}

SOLAR_PARAMETERS = {
    "dataset": "merra2",
    "capacity": 1.0,
    "system_loss": 0.1,
    "tracking": 0,
    "tilt": 35,
    "azim": 180,
}


# define the parameters for the ninja api
# don't edit this unless you have a commercial account with higher rate limits!

API_BASE = "https://www.renewables.ninja/api/data"
TOKEN_ENV_VAR = "NINJA_TOKEN"
LOG_PATH = Path(__file__).resolve().with_name("get_ninja_country.log")

REQUESTS_PER_MINUTE = 6
REQUESTS_PER_HOUR = 50
REQUEST_TIMEOUT_SECONDS = 300
MAX_DOWNLOAD_ATTEMPTS = 5



# all data you download with this script is subject to the standard renewables.ninja license
# please cite the data as outlined in the downloaded files, and please email 
# contact@renewables.ninja if you wish to this script or any data for commercial purposes

CITATION_LINES = {
    "solar": (
        "# Renewables.ninja Solar photovoltaic power (PV) (Point API) - "
        "License: https://creativecommons.org/licenses/by-nc/4.0/ - "
        "Reference: https://doi.org/10.1016/j.energy.2016.08.060"
    ),
    "wind": (
        "# Renewables.ninja Wind power (Point API) - "
        "License: https://creativecommons.org/licenses/by-nc/4.0/ - "
        "Reference: https://doi.org/10.1016/j.energy.2016.08.068"
    ),
}


# now it's go time


def positive_integer(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    # define the command line options
    parser = argparse.ArgumentParser(
        description=(
            "Select representative locations from a coordinate CSV and "
            "download hourly Renewables.ninja data."
        )
    )
    parser.add_argument("--input", required=True, type=Path, help="Input CSV file.")
    parser.add_argument("--n", required=True, type=positive_integer, help="Number of sites per technology.")
    parser.add_argument(
        "--year",
        type=int,
        default=2025,
        help="Calendar year to download (default: 2025).",
    )
    parser.add_argument("--wind", action="store_true", help="Download wind data.")
    parser.add_argument("--solar", action="store_true", help="Download solar PV data.")
    parser.add_argument("--both", action="store_true", help="Download wind and solar PV data.")
    parser.add_argument(
        "--bias",
        action="store_true",
        help="Power-transform hourly values so their mean equals the input CSV capacity factor.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Output directory (default: the input CSV directory).",
    )
    return parser


def configure_logging() -> logging.Logger:
    # write progress to the screen and log file
    logger = logging.getLogger("get_ninja_country")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file_handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    logger.addHandler(console_handler)
    return logger


def selected_technologies(args: argparse.Namespace, parser: argparse.ArgumentParser) -> list[str]:
    # turn the technology flags into one list
    if args.both and (args.wind or args.solar):
        parser.error("use either --both or --wind/--solar, not both forms together")

    technologies: list[str] = []
    if args.both or args.wind:
        technologies.append("wind")
    if args.both or args.solar:
        technologies.append("solar")
    if not technologies:
        parser.error("select --wind, --solar, both flags together, or --both")
    return technologies


def validate_year(year: int) -> None:
    # allow complete calendar years for simplicity
    latest_year = datetime.now().year - 1
    # dangerously assume we have updated the ninja to have the latest calendar year
    if year < 1980 or year > latest_year:
        raise ValueError(f"--year must be between 1980 and {latest_year}")


def read_input_data(
    input_path: Path,
    technologies: list[str],
    require_capacity_factors: bool = False,
) -> pd.DataFrame:
    # read only the columns needed for this run
    if not input_path.exists():
        raise ValueError(f"input CSV does not exist: {input_path}")
    if not input_path.is_file():
        raise ValueError(f"input path is not a file: {input_path}")
    if input_path.suffix.lower() != ".csv":
        raise ValueError("--input must be a CSV file")

    try:
        header = pd.read_csv(input_path, nrows=0)
    except Exception as exc:
        raise ValueError(f"could not read input CSV header: {exc}") from exc

    required = {"lon", "lat"}
    capacity_factor_columns = {f"{technology}_cf" for technology in technologies}
    if require_capacity_factors:
        required.update(capacity_factor_columns)
    missing = sorted(required.difference(header.columns))
    if missing:
        raise ValueError(f"input CSV is missing required column(s): {', '.join(missing)}")

    columns_to_read = required | capacity_factor_columns.intersection(header.columns)
    try:
        data = pd.read_csv(input_path, usecols=sorted(columns_to_read))
    except Exception as exc:
        raise ValueError(f"could not read input CSV: {exc}") from exc

    if data.empty:
        raise ValueError("input CSV contains no locations")
    if data.isna().any().any():
        bad_columns = ", ".join(data.columns[data.isna().any()])
        raise ValueError(f"input CSV contains missing values in: {bad_columns}")
    if not data["lon"].between(-180, 180).all():
        raise ValueError("longitude values must be between -180 and 180")
    if not data["lat"].between(-90, 90).all():
        raise ValueError("latitude values must be between -90 and 90")

    for technology in technologies:
        cf_column = f"{technology}_cf"
        if cf_column in data and not data[cf_column].between(0, 1).all():
            raise ValueError(f"{cf_column} values must be between 0 and 1")

    data = data.reset_index(drop=True)
    # keep the original data row as the site id
    data["row_number"] = np.arange(1, len(data) + 1)
    data["id"] = data["row_number"].map(lambda value: f"row_{value:06d}")
    return data


def clustering_features(data: pd.DataFrame, cf_column: str | None = None) -> np.ndarray:
    # convert the coordinates into approximate kilometres
    earth_radius_km = 6371.0088
    lon_radians = np.radians(data["lon"].to_numpy(dtype=float))
    lat_radians = np.radians(data["lat"].to_numpy(dtype=float))
    mean_latitude = float(lat_radians.mean())

    x_km = earth_radius_km * math.cos(mean_latitude) * lon_radians
    y_km = earth_radius_km * lat_radians
    x_centered = x_km - x_km.mean()
    y_centered = y_km - y_km.mean()

    # use one scale to keep the map shape
    geographic_scale = math.sqrt(float(np.mean(x_centered**2 + y_centered**2)))
    if geographic_scale == 0:
        geographic_scale = 1.0

    features = [
        x_centered / geographic_scale,
        y_centered / geographic_scale,
    ]
    if cf_column is not None and cf_column in data:
        cf = data[cf_column].to_numpy(dtype=float)
        # scale capacity factor to give it fair weight in the cluster choices
        cf_centered = cf - cf.mean()
        cf_scale = float(cf_centered.std())
        if cf_scale == 0:
            cf_scaled = np.zeros_like(cf_centered)
        else:
            cf_scaled = cf_centered / cf_scale
        features.append(cf_scaled)

    return np.column_stack(features)


def select_representative_sites(
    data: pd.DataFrame,
    technology: str,
    n_sites: int,
) -> pd.DataFrame:
    # group locations by geography and capacity factor
    n_sites = min(n_sites, len(data))
    if n_sites == len(data):
        return (
            data.drop_duplicates(subset=["lon", "lat"])
            .sort_values("row_number")
            .reset_index(drop=True)
        )

    features = clustering_features(data, f"{technology}_cf")
    model = KMeans(n_clusters=n_sites, random_state=0, n_init=10, algorithm="lloyd")
    labels = model.fit_predict(features)

    representative_indices: list[int] = []
    for cluster_number in np.unique(labels):
        # choose the real row nearest each centre
        members = np.flatnonzero(labels == cluster_number)
        distances = np.sum((features[members] - model.cluster_centers_[cluster_number]) ** 2, axis=1)
        representative_indices.append(int(members[int(np.argmin(distances))]))

    representatives = data.iloc[representative_indices].copy()
    return (
        representatives.drop_duplicates(subset=["lon", "lat"])
        .sort_values("row_number")
        .reset_index(drop=True)
    )


class PersistentRateLimiter:
    request_pattern = re.compile(r"API_REQUEST at=(.+)$")

    def __init__(self, log_path: Path, logger: logging.Logger) -> None:
        self.log_path = log_path
        self.logger = logger
        self.requests: deque[float] = deque()
        self._load_recent_requests()

    def _load_recent_requests(self) -> None:
        # restore recent request times from the log
        if not self.log_path.exists():
            return
        cutoff = time.time() - 3600
        for line in self.log_path.read_text(encoding="utf-8", errors="replace").splitlines():
            match = self.request_pattern.search(line)
            if not match:
                continue
            try:
                parsed_time = datetime.fromisoformat(match.group(1).replace("Z", "+00:00"))
                if parsed_time.tzinfo is None:
                    parsed_time = parsed_time.replace(tzinfo=timezone.utc)
                timestamp = parsed_time.timestamp()
            except ValueError:
                continue
            if timestamp >= cutoff:
                self.requests.append(timestamp)

    def acquire(self) -> None:
        # wait until both api limits allow a request
        while True:
            now = time.time()
            while self.requests and self.requests[0] <= now - 3600:
                self.requests.popleft()

            recent_minute = [stamp for stamp in self.requests if stamp > now - 60]
            waits = [0.0]
            if len(recent_minute) >= REQUESTS_PER_MINUTE:
                waits.append(recent_minute[-REQUESTS_PER_MINUTE] + 60 - now)
            if len(self.requests) >= REQUESTS_PER_HOUR:
                waits.append(self.requests[-REQUESTS_PER_HOUR] + 3600 - now)

            wait_seconds = max(waits)
            if wait_seconds <= 0:
                break
            wait_seconds = math.ceil(wait_seconds + 1)
            self.logger.info("Rate limit reached; waiting %s seconds", wait_seconds)
            time.sleep(wait_seconds)

        requested_at = time.time()
        self.requests.append(requested_at)
        request_timestamp = datetime.fromtimestamp(
            requested_at,
            timezone.utc,
        ).strftime("%Y-%m-%d %H:%M:%S")
        self.logger.info("API_REQUEST at=%s", request_timestamp)


def parse_api_csv(text: str) -> pd.DataFrame:
    # skip api notes and read the hourly table
    try:
        frame = pd.read_csv(io.StringIO(text), comment="#")
    except Exception as exc:
        raise ValueError(f"could not parse API CSV response: {exc}") from exc

    if list(frame.columns) != ["time", "electricity"]:
        raise ValueError(f"unexpected API CSV columns: {list(frame.columns)}")
    if frame.empty:
        raise ValueError("API returned no hourly data")

    frame["time"] = pd.to_datetime(frame["time"], errors="raise")
    frame["electricity"] = pd.to_numeric(frame["electricity"], errors="raise")
    if frame["time"].duplicated().any():
        raise ValueError("API returned duplicate timestamps")
    if not frame["electricity"].between(0, 1).all():
        raise ValueError("API returned capacity factors outside [0, 1]")
    return frame


def validate_complete_year(frame: pd.DataFrame, year: int) -> None:
    # check every hour in the year is present
    expected_times = pd.date_range(
        start=f"{year}-01-01 00:00",
        end=f"{year}-12-31 23:00",
        freq="h",
    )
    actual_times = pd.DatetimeIndex(frame["time"])
    if not actual_times.equals(expected_times):
        raise ValueError(
            f"hourly data are not a complete {year} calendar year "
            f"(expected {len(expected_times)} rows, found {len(actual_times)})"
        )


def solve_bias_exponent(values: np.ndarray, target_mean: float) -> float:
    # find the power that matches the target mean
    values = np.asarray(values, dtype=float)
    if values.size == 0:
        raise ValueError("cannot bias-correct an empty series")
    if np.any((values < 0) | (values > 1)):
        raise ValueError("bias correction requires values in [0, 1]")
    if not 0 <= target_mean <= 1:
        raise ValueError("bias correction target must be in [0, 1]")

    original_mean = float(values.mean())
    if math.isclose(original_mean, target_mean, rel_tol=0, abs_tol=1e-12):
        return 1.0

    minimum_mean = float(np.mean(values == 1))
    maximum_mean = float(np.mean(values > 0))
    tolerance = 1e-12
    if target_mean <= minimum_mean + tolerance or target_mean >= maximum_mean - tolerance:
        raise ValueError(
            "target capacity factor is outside the range achievable by a positive power transform "
            f"({minimum_mean:.6f}, {maximum_mean:.6f})"
        )

    def difference(exponent: float) -> float:
        return float(np.mean(np.power(values, exponent))) - target_mean

    lower = 1e-8
    upper = 1.0
    if difference(upper) > 0:
        while difference(upper) > 0 and upper < 1e8:
            upper *= 2
    else:
        upper = 1.0

    if difference(lower) * difference(upper) > 0:
        raise ValueError("could not bracket a bias-correction exponent")

    for _ in range(100):
        # narrow the range until the mean matches
        midpoint = (lower + upper) / 2
        result = difference(midpoint)
        if abs(result) <= 1e-12:
            return midpoint
        if result > 0:
            lower = midpoint
        else:
            upper = midpoint
    return (lower + upper) / 2


def apply_bias_correction(frame: pd.DataFrame, target_mean: float) -> tuple[pd.DataFrame, float]:
    exponent = solve_bias_exponent(frame["electricity"].to_numpy(dtype=float), target_mean)
    corrected = frame.copy()
    corrected["electricity"] = np.power(corrected["electricity"].to_numpy(dtype=float), exponent)
    return corrected, exponent


def coordinate_text(value: float) -> str:
    return f"{value:.6f}".rstrip("0").rstrip(".")


def site_output_path(
    site_directory: Path,
    input_stem: str,
    technology: str,
    lon: float,
    lat: float,
) -> Path:
    filename = (
        f"renewables_ninja_{technology}_{input_stem}_"
        f"{coordinate_text(lon)}_{coordinate_text(lat)}.csv"
    )
    return site_directory / filename


def download_api_frame(
    session: requests.Session,
    limiter: PersistentRateLimiter,
    technology: str,
    lon: float,
    lat: float,
    year: int,
    logger: logging.Logger,
) -> pd.DataFrame:
    endpoint = "pv" if technology == "solar" else "wind"
    parameters = {
        "lat": lat,
        "lon": lon,
        "date_from": f"{year}-01-01",
        "date_to": f"{year}-12-31",
        "format": "csv",
        **(SOLAR_PARAMETERS if technology == "solar" else WIND_PARAMETERS),
    }

    for attempt in range(1, MAX_DOWNLOAD_ATTEMPTS + 1):
        limiter.acquire()
        try:
            response = session.get(
                f"{API_BASE}/{endpoint}",
                params=parameters,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            if attempt == MAX_DOWNLOAD_ATTEMPTS:
                raise RuntimeError(f"API request failed after {attempt} attempts: {exc}") from exc
            delay = min(60, 2**attempt)
            logger.warning("Request failed (%s); retrying in %s seconds", exc, delay)
            time.sleep(delay)
            continue

        if response.status_code == 200:
            frame = parse_api_csv(response.text)
            validate_complete_year(frame, year)
            return frame

        if response.status_code == 429 or 500 <= response.status_code < 600:
            if attempt == MAX_DOWNLOAD_ATTEMPTS:
                raise RuntimeError(
                    f"API returned HTTP {response.status_code} after {attempt} attempts: "
                    f"{response.text[:300]}"
                )
            retry_after = response.headers.get("Retry-After")
            try:
                delay = float(retry_after) if retry_after else min(60, 2**attempt)
            except ValueError:
                delay = min(60, 2**attempt)
            logger.warning("API returned HTTP %s; retrying in %.1f seconds", response.status_code, delay)
            time.sleep(delay)
            continue

        raise RuntimeError(f"API returned HTTP {response.status_code}: {response.text[:500]}")

    raise AssertionError("download retry loop exited unexpectedly")


def save_site_file(
    output_path: Path,
    frame: pd.DataFrame,
    site: pd.Series,
    technology: str,
    year: int,
    bias_exponent: float | None,
) -> None:
    # write through a temporary file to avoid partial output
    temporary_path = output_path.with_suffix(output_path.suffix + ".tmp")
    with temporary_path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(CITATION_LINES[technology] + "\n")
        handle.write(f"# technology={technology}, year={year}, id={site['id']}\n")
        location_metadata = (
            f"# lon={coordinate_text(float(site['lon']))}, "
            f"lat={coordinate_text(float(site['lat']))}"
        )
        cf_column = f"{technology}_cf"
        if cf_column in site.index:
            location_metadata += f", input_cf={float(site[cf_column]):.10f}"
        handle.write(location_metadata + "\n")
        if bias_exponent is None:
            handle.write("# bias_applied=false\n")
        else:
            handle.write(f"# bias_applied=true, exponent={bias_exponent:.12g}\n")
        frame.to_csv(handle, index=False, date_format="%Y-%m-%d %H:%M", float_format="%.10f")
    temporary_path.replace(output_path)


def load_site_file(path: Path, technology: str, year: int) -> pd.DataFrame:
    text = path.read_text(encoding="utf-8")
    if not text.startswith(CITATION_LINES[technology] + "\n"):
        raise ValueError("site file does not contain the expected citation")
    frame = parse_api_csv(text)
    validate_complete_year(frame, year)
    return frame


def get_site_frame(
    session: requests.Session,
    limiter: PersistentRateLimiter,
    output_path: Path,
    site: pd.Series,
    technology: str,
    year: int,
    apply_bias: bool,
    logger: logging.Logger,
) -> pd.DataFrame:
    # reuse a valid file when a run is restarted
    if output_path.exists():
        try:
            frame = load_site_file(output_path, technology, year)
            logger.info("Reusing existing site file %s", output_path.name)
            return frame
        except Exception as exc:
            logger.warning("Existing site file is invalid and will be replaced: %s", exc)

    logger.info(
        "Downloading %s for %s at lon=%s lat=%s",
        technology,
        site["id"],
        coordinate_text(float(site["lon"])),
        coordinate_text(float(site["lat"])),
    )
    frame = download_api_frame(
        session=session,
        limiter=limiter,
        technology=technology,
        lon=float(site["lon"]),
        lat=float(site["lat"]),
        year=year,
        logger=logger,
    )

    exponent: float | None = None
    if apply_bias:
        # adjust the annual mean without exceeding one
        original_mean = float(frame["electricity"].mean())
        frame, exponent = apply_bias_correction(frame, float(site[f"{technology}_cf"]))
        logger.info(
            "Applied bias correction to %s with exponent %.4f ~ mean %.4f -> %.4f",
            site["id"],
            exponent,
            original_mean,
            float(frame["electricity"].mean()),
        )

    save_site_file(output_path, frame, site, technology, year, exponent)
    return frame


def write_merged_file(
    output_path: Path,
    representatives: pd.DataFrame,
    site_frames: list[pd.DataFrame],
    technology: str,
) -> None:
    # check that all sites share the same time index
    if not site_frames:
        raise ValueError("there are no site files to merge")

    reference_times = site_frames[0]["time"].reset_index(drop=True)
    for site, frame in zip(representatives.itertuples(index=False), site_frames):
        if not reference_times.equals(frame["time"].reset_index(drop=True)):
            raise ValueError(f"timestamps for {site.id} do not match the other site files")

    temporary_path = output_path.with_suffix(output_path.suffix + ".tmp")
    with temporary_path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(CITATION_LINES[technology] + "\n")
        # write site details as the first three rows
        writer = csv.writer(handle)
        writer.writerow(["lon", *(coordinate_text(float(value)) for value in representatives["lon"])])
        writer.writerow(["lat", *(coordinate_text(float(value)) for value in representatives["lat"])])
        writer.writerow(["id", *representatives["id"].tolist()])

        value_arrays = [frame["electricity"].to_numpy(dtype=float) for frame in site_frames]
        for row_number, timestamp in enumerate(reference_times):
            writer.writerow(
                [
                    timestamp.strftime("%Y-%m-%d %H:%M"),
                    *(f"{values[row_number]:.10f}" for values in value_arrays),
                ]
            )
    temporary_path.replace(output_path)


def run(args: argparse.Namespace, parser: argparse.ArgumentParser, logger: logging.Logger) -> list[Path]:
    # validate inputs before starting any api calls
    technologies = selected_technologies(args, parser)
    validate_year(args.year)
    input_path = args.input.expanduser().resolve()
    data = read_input_data(
        input_path,
        technologies,
        require_capacity_factors=args.bias,
    )

    requested_sites = args.n
    n_sites = min(requested_sites, len(data))
    if requested_sites > len(data):
        logger.warning(
            "Requested %s sites but the input contains %s locations; using every unique location",
            requested_sites,
            len(data),
        )

    token = os.environ.get(TOKEN_ENV_VAR, "").strip()
    if not token:
        raise ValueError(f"environment variable {TOKEN_ENV_VAR} is not set")

    output_directory = (
        args.output_dir.expanduser().resolve() if args.output_dir else input_path.parent
    )
    output_directory.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    session.headers.update({"Authorization": f"Token {token}"})
    limiter = PersistentRateLimiter(LOG_PATH, logger)
    outputs: list[Path] = []

    for technology in technologies:
        # select and download sites for each technology
        logger.info("Clustering %s locations for %s", len(data), technology)
        representatives = select_representative_sites(data, technology, n_sites)
        actual_sites = len(representatives)
        if actual_sites < n_sites:
            logger.warning(
                "Clustering selected %s unique locations instead of %s; duplicate locations were removed",
                actual_sites,
                n_sites,
            )

        variant = "bias" if args.bias else "unbiased"
        site_directory = output_directory / (
            f"renewables_ninja_{input_path.stem}_{technology}_{args.year}_{variant}_sites"
        )
        site_directory.mkdir(parents=True, exist_ok=True)

        site_frames: list[pd.DataFrame] = []
        for _, site in representatives.iterrows():
            path = site_output_path(
                site_directory,
                input_path.stem,
                technology,
                float(site["lon"]),
                float(site["lat"]),
            )
            site_frames.append(
                get_site_frame(
                    session=session,
                    limiter=limiter,
                    output_path=path,
                    site=site,
                    technology=technology,
                    year=args.year,
                    apply_bias=args.bias,
                    logger=logger,
                )
            )

        bias_suffix = "_bias" if args.bias else ""
        merged_path = output_directory / (
            f"renewables_ninja_{technology}_{input_path.stem}_{args.year}_n{actual_sites}{bias_suffix}.csv"
        )
        # combine the site files into the final output
        write_merged_file(merged_path, representatives, site_frames, technology)
        logger.info("Wrote merged output %s", merged_path)
        outputs.append(merged_path)

    return outputs


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    logger = configure_logging()
    try:
        outputs = run(args, parser, logger)
    except (ValueError, RuntimeError, OSError) as exc:
        logger.error("%s", exc)
        return 1

    for output in outputs:
        print(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())

# oxo
