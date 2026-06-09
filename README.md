# Ninja country downloader

`get_ninja_country.py` is designed to automate the downloading of country-scale time-series of renewable energy capacity factors.  It selects representative sites from an input CSV file, downloads hourly data from [Renewables.ninja](https://www.renewables.ninja), and combines the site series into one CSV file.

It is not limited to downloading countries, you can use it to download any set of wind or solar farm output data.


## Installation

Python 3.10 or newer.

```text
python -m pip install -r requirements.txt
```


## Input files

The code takes an input CSV file that contains the coordinates of wind and/or solar farms, and optionally their expected productivity.  

As a minimum, you can run the code with just geographical coordinates (one per row):
```csv
"lon","lat"
80.1958,9.8292
80.2042,9.8292
```

Or if you wish to bias-correct your data, your input file must include the capacity factors for wind and/or solar farms:
```csv
"lon","lat","solar_cf","wind_cf"
80.1958,9.8292,0.1839,0.3847
80.2042,9.8292,0.1839,0.3784
80.2125,9.8292,0.1841,0.3676
80.2208,9.8292,0.1841,0.3675
```

Note, if you have high resolution gridded data for a country, it might be infeasible to download all locations, so the code can generate representative clustered locations for you.


## API token

An API token is required to download data from [Renewables.ninja](https://www.renewables.ninja).  To use this script, you must first register for a Renewables.ninja account, and copy your API key from your user profile.  

It is not good security practice to save your token within the script, so it is read from the `NINJA_TOKEN` environment variable, which you must set in your console before running the script.

In Windows Command Prompt, run:

```bat
set NINJA_TOKEN=your_token_here
```

In macOS Terminal, run:

```bash
export NINJA_TOKEN="your_token_here"
```


## Usage

This repository includes two example input files, giving coordinates for Singapore and South Korea on a fine grid.  For Singapore (`example_input_SG.csv`) this only contains the coordinates.  For South Korea (`example_input_KR.csv`) this also includes the expected average capacity factor for wind and solar farms.  These data are taken from the forthcoming paper Bamisile et al., *Decarbonizing South and East Asia electrification with wind power and solar PV requires high subsidy*.

### Example 1:

To download 5 representative wind sites in Singapore using the default year of 2025:

```text
python get_ninja_country.py --input "example_input_SG.csv" --wind --n 6
```

If you want to download every site that is listed in the file, use `--n all`, or set `--n` to the number of rows in your file.

### Example 2:

To download 3 sites for wind and 3 for solar in South Korea for the year 2020:

```text
python get_ninja_country.py --input "example_input_KR.csv" --wind --solar --n 3 --year 2020
```

Note that different locations can be chosen for wind and for solar, as they are clustered individually.

### Example 3:

If your input file contains expected capacity factors, you can automatically bias correct the data to give that annual average by setting `--bias`:

```text
python get_ninja_country.py --input "example_input_KR.csv" --wind --n 3 --year 2020 --bias
```

## Available options

- `--input FILE`: input CSV containing `lon` and `lat` columns. To use the `--bias` option, it must also contain `wind_cf` or `solar_cf` columns.
- `--wind`, `--solar`, or `--both`: technology selection.
- `--n N`: positive number of representative sites selected per technology. If `N` exceeds the number of input locations, every unique location is used.
- `--year YEAR`: complete calendar year to download. The allowed range is 1980 through the previous calendar year. The default is 2025.
- `--bias`: transform each site's hourly values so their annual mean equals the capacity factor in the input CSV.
- `--output-dir DIRECTORY`: output location. The default is the input CSV's directory.

The technology options for the wind or solar farms that are simulated are set to the defaults used in Renewables.ninja.  These can be changed by setting your preferred values towards the top of `get_ninja_country.py`.

By default:
- Wind: capacity 1 kW, 100 m hub height, Vestas V80 2000 turbine.
- Solar: MERRA-2, capacity 1 kW, 10% system loss, fixed 35-degree tilt, 180-degree azimuth, and no tracking.


## Method for selecting representative sites

Longitude and latitude are projected to approximate kilometre coordinates using an equirectangular projection centred on the input data's mean latitude. The two geographic axes are divided by one shared root-mean-square distance, which preserves the geographic aspect ratio. Capacity factor is centred and divided by its standard deviation.

When the selected technology's capacity-factor column is present, this gives geography as a whole and capacity factor equal average weight in the K-means distance calculation. Without that optional column, clustering uses geography alone. The method avoids treating longitude degrees as the same physical distance at every latitude and avoids exaggerating a country's narrow geographic dimension. K-means uses a fixed random seed for reproducible selection. Each centroid is replaced by the nearest actual CSV row within its cluster.

Site IDs are the one-based data-row numbers from the input CSV, excluding the header, formatted as `row_000001`.

When both technologies are requested, clustering and output are handled separately because the representative sites can differ when capacity-factor columns are present.


## Method for bias correction

If you wish to obtain a specific annual average capacity factor, the `--bias` flag will obtain the target capacity factor for each site in your input data. This is done with a simple exponential approach, numerically finding a  positive exponent `p` satisfying:

```text
mean(cf_hourly ^ p) = cf_target
```

This allows the annual average (`cf_target`) to be adjusted while ensuring the transformed values remain in `[0, 1]`. The script reports an error if the target capacity factor is below 0 or above 1, or the bias correction is otherwise mathematically impossible.

## Outputs and restart behaviour

Individual site files are stored in a directory that is specific to the technology, year, and bias. Their names follow:

```text
renewables_ninja_{technology}_{input_name}_{longitude}_{latitude}.csv
```

Existing valid site files are reused, so an interrupted run can be restarted without repeating completed API calls.

Merged files are named as:

```text
renewables_ninja_{technology}_{input_name}_{year}_n{N}.csv
renewables_ninja_{technology}_{input_name}_{year}_n{N}_bias.csv
```

The merged file has three header rows: longitude, latitude, and row ID. The first column below those headers contains hourly UTC timestamps.

If clustering returns the same longitude and latitude more than once, only one copy is downloaded and included. The merged filename reports the actual number of unique sites.

The script enforces at most 6 requests per minute and 50 requests per hour, including across restarted runs by reading request timestamps from `get_ninja_country.log`. Do not run multiple copies of the script at the same time, because separate processes cannot coordinate their waiting.


## Credits and contact

Contact [Iain Staffell](mailto:i.staffell@imperial.ac.uk) for questions about this code.  The ninja country downloader relies upon the [Renewables.ninja](https://www.renewables.ninja) project, developed by Stefan Pfenninger and Iain Staffell. Use the [contact page](https://www.renewables.ninja/about) there if you want more information about Renewables.ninja.

## Citation

If you use the Ninja Country Downloader, code derived from, or data downloaded by it in your work, please cite:

> Stefan Pfenninger and Iain Staffell (2016). Long-term patterns of European PV output using 30 years of validated hourly reanalysis and satellite data. *Energy* 114, 1251-1265. [doi: 10.1016/j.energy.2016.08.060](https://doi.org/10.1016/j.energy.2016.08.060)

and

> Iain Staffell and Stefan Pfenninger (2016). Using bias-corrected reanalysis to simulate current and future wind power output. *Energy*, 114, 1224–1239. [doi: 10.1016/j.energy.2016.08.068](https://dx.doi.org/10.1016/j.energy.2016.08.068)

## License

BSD-3-Clause
