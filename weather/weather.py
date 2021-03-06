#!/usr/bin/python
"""
Module for downloading and using ECMWF weather data.

There are two important classes:
    * WeatherApi: used for downloading GRIB files from MARS
    * WeatherExtractor: used for querying weather data from a pre-downloaded GRIB file

Example:
    Examples of class usages are given in class docstring.

Todo:
    * add interpolation capability to WeatherExtractor._aggregate_points
"""
from __future__ import print_function
import datetime
import math
import json
import pickle
from collections import defaultdict

import numpy as np
import pandas as pd

"""
    Best estimation for actual weather is forecast with a base date on the current day.

    Parameter name:                 Short name:

    2 metre dewpoint temperature        2d
    2 metre temperature                 2t
    10 metre U wind component           10u
    10 metre V wind component           10v
    Precipitation type**                ptype
    Snow depth                          sd
    Snow fall                           sf
    Sunshine duration*                  sund
    Surface pressure                    sp
    Total cloud cover                   tcc
    Total precipitation*                tp
    Visibility [m]                      vis

    Wind speed*** [m/s]                 ws
    Relative humidity*** [%]            rh

    *accumulated from the beginning of the forecast
    **mean aggregation of this parameter makes no sense
    ***calculated parameter

    Precipitation type (ptype) code table:
        0 = No precipitation
        1 = Rain
        3 = Freezing rain (i.e. supercooled)
        5 = Snow
        6 = Wet snow (i.e. starting to melt)
        7 = Mixture of rain and snow
        8 = Ice pellets

    Warning:
        * after 2015-5-13 number of parameters changes
"""
class WeatherExtractor:
    """
    Interface for extracting weather data from pre-downloaded GRIB file.
    Each GRIB file is a collection of self contained weather messages.

    It supports actual weather queries ( via .get_actual(...) ) and forecasted weather
    queries ( via .get_forecast(...) )

    Examples
        $ we = WeatherExtractor()
        $ we.load('example_data.grib')

        Queries about actual weather have the following format:

            $ wa.get_actual(from_date, to_date, aggtime)

            Where:
                from_date, to_date: time window in days
                aggtime: aggregation of weather data on different levels:
                    aggtime='hour': aggregate by hour
                    aggtime='day': aggregation by day
                    aggtime='week': aggregation by week

        Queries about forecasted weather have the following format:

            $ wa.get_forecast(base_date, from_date, to_date, aggtime)

            Where:
                from_date, to_date: time window in days
                aggtime: aggregation of weather data on different levels:
                    aggtime='hour': aggregate by hour
                    aggtime='day': aggregation by day
                    aggtime='week': aggregation by week

    """

    def __init__(self):
        self.grib_msgs = None

    def _load_from_grib(self, filepath, grib_reader):
        """ Load measurements from GRIB file. """
        # load grib messages
        grib_messages = []
        lats, lons = None, None

        if grib_reader[1] == 'eccodes':
            grbs = grib_reader[0](filepath)
            for i in range(len(grbs)):
                grib_msg = grbs.next()

                if lats is None:
                    lats = grib_msg['latitudes'].flatten()
                    lons = grib_msg['longitudes'].flatten()

                grib_messages.append({
                    'shortName': grib_msg['shortName'],
                    'values': grib_msg['values'].flatten(),
                    'validDateTime': WeatherExtractor._str_to_datetime(
                        str(grib_msg['date']) + str(grib_msg['time'])),
                    'validityDateTime': WeatherExtractor._str_to_datetime(
                        str(grib_msg['validityDate']) + str(grib_msg['validityTime'])),
                    'lats': lats,
                    'lons': lons,
                    'type': grib_msg['marsType']  # forecast or actual
                })
            grbs.close()
        else:
            grbs = grib_reader[0](filepath)
        
            lats, lons = grbs.message(1).latlons()
            lats, lons = lats.flatten(), lons.flatten()
            
            grbs.rewind()
            for grib_msg in grbs:
                grib_messages.append({
                    'shortName': grib_msg.shortName,
                    'values': grib_msg.values.flatten(),
                    'validDateTime': grib_msg.validDate,
                    'validityDateTime': WeatherExtractor._str_to_datetime(
                        str(grib_msg.validityDate) + str(grib_msg.validityTime)),
                    'lats': lats,
                    'lons': lons,
                    'type': grib_msg.marsType  # forecast or actual
                })
            grbs.close()
        return pd.DataFrame.from_dict(grib_messages)

    def _load_from_pkl(self, filepath):
        """ Load already processed pandas.DataFrame. """
        with open(filepath, 'rb') as f:
            return pickle.load(f)

    def _load_from_owmjson(self, filepath):
        """ Load measurements from OpenWeatherMap json response. """
        # Convert to the same 'grib messages' format as ecmwf data
        grib_messages = []
        
        with open(filepath, 'r') as f:
            d = json.loads(f.read())
        
        lats = [d['city']['coord']['lat']]
        lons = [d['city']['coord']['lon']]
        validDateTime = pd.to_datetime(d['list'][0]['dt_txt'])

        def __add_msg(name, value, validityDateTime):
            grib_messages.append({
                'shortName': name,
                'values': np.array([value]),
                'validDateTime': pd.to_datetime(validDateTime.date()),
                'validityDateTime': validityDateTime,
                'lats': lats,
                'lons': lons,
                'type': 'fc'
            })

        # Total precipitation is accumulated parameter in ECMWF (in [m])
        # make it accumulated also for OWM
        tp_acc = 0.0

        for msg in d['list']:
            validityDateTime = pd.to_datetime(msg['dt_txt'])
            
            __add_msg('2t', msg['main']['temp'], validityDateTime)
            __add_msg('rh', msg['main']['humidity'] / 100.0, validityDateTime)
            __add_msg('sp', msg['main']['grnd_level'] / 100.0, validityDateTime)
            __add_msg('tcc', msg['clouds']['all'] / 100.0 if 'clouds' in msg else 0.0, validityDateTime)
            __add_msg('ws', msg['wind']['speed'] if 'wind' in msg else 0.0, validityDateTime)
            if 'rain' in msg and '3h' in msg['rain']:
                tp_acc += msg['rain']['3h'] / 1000.0 # in [mm] originally
            __add_msg('tp', tp_acc, validityDateTime)
            __add_msg('sf', msg['snow']['3h'] / 100.0 if 'snow' in msg else 0.0, validityDateTime)
            
        return pd.DataFrame.from_dict(grib_messages)

    def load(self, filepaths, format=None):
        """
        Load weather data from grib file obtained via API request or from
        the pickled pandas.DataFrame.

        Arguments:
            filepaths (list): list of paths to files containing weather data
            format (str): one of the following:
                'grib': files are stored in grib format
                'pkl': files are stored in binary form (pickled)
                'owm': files are stored in OpenWeatherMap json format

                if format is not specified it is automatically inferred from file prefix
                (.grib, .pkl or .json)
        
        Warning:
            after 2015-5-13 number of parameters increases from 11 to 15 and
            additional parameter 'ptype' which disturbs the indexing
            (because of inconsistent 'validDateTime') sneaks in
        """
        if not isinstance(filepaths, list):
            filepaths = [filepaths]  # wrap in list
        
        grib_reader = None
        if format is None:
            if all(f.endswith('.grib') for f in filepaths):
                format = 'grib'

                try:
                    import eccodes
                    grib_reader = (eccodes.GribFile, 'eccodes')
                except ImportError:
                    import pygrib
                    grib_reader = (pygrib.open, 'pygrib')
                print('Using ', grib_reader[1], ' as GRIB decoder.')

            elif all(f.endswith('.pkl') for f in filepaths):
                format = 'pkl'
            elif all(f.endswith('.json') for f in filepaths):
                format = 'owm'
            else:
                raise ValueError("Could not infer the file format.")

        for filepath in filepaths:
            curr_msgs = None

            if format == 'grib':
                curr_msgs = self._load_from_grib(filepath, grib_reader)
            elif format == 'pkl':
                curr_msgs = self._load_from_pkl(filepath)
            elif format == 'owm':
                curr_msgs = self._load_from_owmjson(filepath)
            else:
                raise ValueError("Format %s not recognized" % format)

            # append messages
            if self.grib_msgs is None:
                self.grib_msgs = curr_msgs
            else:
                self.grib_msgs = pd.concat([self.grib_msgs, curr_msgs])

            # reset index
            self.grib_msgs.reset_index(drop=True, inplace=True)

        # extend the set of parameters if data is from grib files
        if format == 'grib':
            self.grib_msgs = WeatherExtractor._extend_parameters(self.grib_msgs)

        # index by base date (date when the forecast was made)
        self.grib_msgs.set_index('validDateTime', drop=False, inplace=True)
        self.grib_msgs.sort_index(inplace=True)

    def store(self, filepath):
        if not filepath.endswith('.pkl'):
            filepath += '.pkl'
        print("Saving weather data to: %s" % filepath)
        with open(filepath, 'wb') as f:
            pickle.dump(self.grib_msgs, f)

    @staticmethod
    def _extend_parameters(grib_msgs):
        """ Extend the set of weather parameters with ones calculated
        from base parameters.
        """
        print("Extending parameters...")
        curr_params = np.unique(grib_msgs.shortName)
        # calculate Wind speed [ws] parameter
        if ('10u' in curr_params) and '10v' in curr_params and not 'ws' in curr_params:
            # print "Calculating parameter Wind speed (ws)"
            grp = grib_msgs[(grib_msgs.shortName == '10u') | (grib_msgs.shortName == '10v')].reset_index(drop=True).groupby(['validDateTime', 'validityDateTime'])

            new_msgs = []
            for group in grp.groups:
                tf = grp.get_group(group)
                new_msgs.append({
                    'shortName': u'ws',
                    'values': np.sqrt(np.sum(v*v for v in tf['values'])),
                    'validDateTime': tf['validDateTime'].iloc[0],
                    'validityDateTime': tf['validityDateTime'].iloc[0],
                    'lats': tf['lats'].iloc[0],
                    'lons': tf['lons'].iloc[0],
                    'type': tf['type'].iloc[0]
                })

            new_msgs = pd.DataFrame.from_dict(new_msgs)
            grib_msgs = grib_msgs.append(new_msgs)

        # calculate Relative humidity (rh) parameter
        if '2t' in curr_params and '2d' in curr_params and not 'rh' in curr_params:
            T0 = 273.15

            # get dewpoint temperature and surface temperature
            grp = grib_msgs[(grib_msgs.shortName == '2t') | (grib_msgs.shortName == '2d')].reset_index(drop=True).groupby(['validDateTime', 'validityDateTime'])

            new_msgs = []
            for group in grp.groups:
                tf = grp.get_group(group)
                T_surface = tf[tf.shortName == '2t'].iloc[0]['values'] - T0
                T_dew = tf[tf.shortName == '2d'].iloc[0]['values'] - T0

                # calculate relative humidity using https://journals.ametsoc.org/doi/pdf/10.1175/BAMS-86-2-225
                rh = 100*(np.exp((17.625*T_dew)/(243.04+T_dew))/np.exp((17.625*T_surface)/(243.04+T_surface)))
                new_msgs.append({
                    'shortName': u'rh',
                    'values': rh,
                    'validDateTime': tf['validDateTime'].iloc[0],
                    'validityDateTime': tf['validityDateTime'].iloc[0],
                    'lats': tf['lats'].iloc[0],
                    'lons': tf['lons'].iloc[0],
                    'type': tf['type'].iloc[0]
                })
            new_msgs = pd.DataFrame.from_dict(new_msgs)
            grib_msgs = grib_msgs.append(new_msgs)

        return grib_msgs

    def _latslons_from_dict(self, points):
        """ Get lattitudes and longtitudes from list of points. """
        assert isinstance(points, list)

        n_points = len(points)
        lats, lons = np.zeros(n_points), np.zeros(n_points)
        for i, point in enumerate(points):
            lats[i], lons[i] = point['lat'], point['lon']
        return (lats, lons)

    def _calc_closest(self, lats, lons, target_lats, target_lons):
        """
        For each point Pi = (lats[i], lons[i]) calculate the closest point Pj = (target_lats[j], target_lons[j])
        according to euclidean distance. In case of a tie take the first point with minimum distance.

        Args:
            lats, lons (np.array(dtype=float)): latitudes and longtitudes of original points
            target_lats, target_lons (np.array(dtype=float)): latitudes and longtitudes of target points

        Returns:
            np.array(dtype=int): array where value at index i represents the index of closest point j
        """
        num_points = lats.shape[0]
        num_target = target_lats.shape[0]

        closest = np.zeros(num_points, dtype=np.int)
        for i in range(num_points):
            best_dist = (lats[i] - target_lats[0])**2 + \
                (lons[i] - target_lons[0])**2
            for j in range(1, num_target):
                curr_dist = (lats[i] - target_lats[j])**2 + \
                    (lons[i] - target_lons[j])**2
                if curr_dist < best_dist:
                    best_dist = curr_dist
                    closest[i] = j
        return closest

    def _interpolate_values(self, values, closest, num_original, num_targets, aggtype):
        """
        Do a value interpolation for given target points according to aggregation type.

        Args:
            values (np.array(dtype=float)): original values
            closest (np.array(dtype=int)):
            num_original (int):
            num_targets (int):
            aggtype (str):

        Returns:
            np.array(dtype=float): interpolated values for target points
        """
        # get interpolated values
        result_values = np.zeros(num_targets)
        if aggtype == 'one':
            for i in range(num_targets):
                result_values[i] = values[closest[i]]
        elif aggtype == 'mean':
            result_count = np.zeros(num_targets)
            for i in range(num_original):
                result_values[closest[i]] += values[i]
                result_count[closest[i]] += 1.
            result_count[result_count == 0] = 1.  # avoid dividing by zero
            result_values /= result_count
        return result_values

    @staticmethod
    def _str_to_datetime(val):
        """ Convert datetime string 'YYYYMMDDHHMM' to datetime object. """
        tmp_date = datetime.date(int(val[:4]), int(val[4:6]), int(val[6:8]))

        time_str = val[8:]
        assert len(time_str) in [1, 3, 4]
        if len(time_str) == 1:
            # midnight - only one number
            return datetime.datetime.combine(tmp_date, datetime.time(int(time_str)))
        elif len(time_str) == 3:
            # hmm format
            return datetime.datetime.combine(tmp_date, datetime.time(int(time_str[:1]), int(time_str[1:])))
        elif len(time_str) == 4:
            # hhmm format
            return datetime.datetime.combine(tmp_date, datetime.time(int(time_str[:2]), int(time_str[2:])))

    def _aggregate_points(self, weather_result, aggloc, aggtype='one', interp_points=None, bounding_box=None):
        """
        Do an interpolation of measurement values for target points (given with target_lats and target_lons)
        from weather_result points.

        Args:
            weather_result (pandas.DataFrame): object containing original measurements and points
            aggloc (str): aggregation level
            aggtype (str): aggregation type, can be one of the following:

                'one' - keep only the value of a grid point which is closest to the target point
                'mean' - calculate the mean value of all grid points closest to the target point

                TODO:
                    'interpolate' - do a kind of ECMWF interpolation

        Returns:
            pandas.DataFrame: resulting object with interpolated points
        """
        assert aggloc in ['grid', 'points', 'country', 'bbox']
        assert aggtype in ['one', 'mean']
        if aggloc == 'points':
            assert interp_points is not None
        assert len(weather_result) > 0

        lats, lons = weather_result['lats'].iloc[0], weather_result['lons'].iloc[0]

        if aggloc == 'bbox':
            assert bounding_box is not None, "bounding box not given"
            assert len(bounding_box) == 2 and len(bounding_box[0]) == 2 and len(bounding_box[1]) == 2, \
                "Wrong bounding box input structure"
            # get bounding box borders
            bb_min_lat = min(bounding_box[0][0], bounding_box[1][0])
            bb_max_lat = max(bounding_box[0][0], bounding_box[1][0])
            bb_min_lon = min(bounding_box[0][1], bounding_box[1][1])
            bb_max_lon = max(bounding_box[0][1], bounding_box[1][1])
            # get data borders
            min_lat, max_lat = min(lats), max(lats)
            min_lon, max_lon = min(lons), max(lons)
            # check if bounding box is within data
            assert min_lat <= bb_min_lat <= max_lat, "bounding box must be within data area"
            assert min_lat <= bb_max_lat <= max_lat, "bounding box must be within data area"
            assert min_lon <= bb_min_lon <= max_lon, "bounding box must be within data area"
            assert min_lon <= bb_max_lon <= max_lon, "bounding box must be within data area"
            # filter out bounding box points
            tmp_lats, tmp_lons = [], []
            for lat, lon in zip(lats, lons):
                if bb_min_lat <= lat <= bb_max_lat and bb_min_lon <= lon <= bb_max_lon:
                    tmp_lats.append(lat)
                    tmp_lons.append(lon)
            assert len(tmp_lats) > 0 and len(tmp_lons) > 0, "bounding box contains no points"
            lats, lons = np.array(tmp_lats), np.array(tmp_lons)

        if aggloc == 'grid':  # no aggregation
            return weather_result
        elif aggloc == 'points':
            target_lats, target_lons = interp_points[0], interp_points[1]
        elif aggloc == 'country':  # center of slovenia
            target_lats, target_lons = np.array([46.1512]), np.array([14.9955])
        elif aggloc == 'bbox': # aggregate over bounding box
            # compute mid point of bounding box
            mid_bbox = [0.5 * (bb_min_lat + bb_max_lat), 0.5 * (bb_min_lon + bb_max_lon)]
            target_lats, target_lons = np.array([mid_bbox[0]]), np.array([mid_bbox[1]])

        if aggtype == 'one':
            # each target point has only one closest grid point
            closest = self._calc_closest(target_lats, target_lons, lats, lons)
        elif aggtype == 'mean':
            # each grid point has only one closest target point
            closest = self._calc_closest(lats, lons, target_lats, target_lons)

        num_original = lats.shape[0]
        num_targets = target_lats.shape[0]

        # create new weather object
        tmp_result = list()

        columns = weather_result.columns
        for raw_row in weather_result.itertuples(index=False):
            row_data = dict()
            for col_pos, col_str in enumerate(columns):
                # affected columns are 'values', 'lats' and 'lons'
                if col_str == 'values':
                    row_data[col_str] = self._interpolate_values(
                        raw_row[col_pos], closest, num_original, num_targets, aggtype)
                elif col_str == 'lats':
                    row_data[col_str] = target_lats
                elif col_str == 'lons':
                    row_data[col_str] = target_lons
                else:
                    row_data[col_str] = raw_row[col_pos]
            tmp_result.append(row_data)

        return pd.DataFrame.from_dict(tmp_result)

    def _aggregate_values(self, weather_result, aggtime):
        """
        Aggregate weather values on hourly, daily or weekly level. Calculate the mean
        value for each measurement point over given time period.

        Serves more as an aggregation example. For more complex aggregations set aggtime='hour'
        and implement own aggregation policy on pandas.DataFrame.

        Args:
            weather_result (pandas.DataFrame): object containing original measurements
            aggtime (str): aggregation level which can be 'hour', 'day' or 'week'

        Returns:
            pandas.DataFrame: resulting object with aggregated values
        """
        assert aggtime in ['hour', 'day', 'week', 'H', 'D', 'W']
        aggtime = {'hour': 'H', 'day': 'D', 'week': 'W'}[aggtime]

        if aggtime == 'H':
            return weather_result

        weather_result.set_index(
            ['validDateTime', 'validityDateTime', 'shortName'], drop=True, inplace=True)

        groups = weather_result.groupby([pd.Grouper(freq='D', level='validDateTime'), pd.Grouper(
            freq=aggtime, level='validityDateTime'), pd.Grouper(level='shortName')])

        tmp_result = groups.apply(
            lambda group:
            pd.Series(
                {
                    'values': group['values'].mean(),
                    'lats': group['lats'].iloc[0],
                    'lons': group['lons'].iloc[0]
                })
        )
        tmp_result.reset_index(drop=False, inplace=True)

        return tmp_result

    def get_actual(self, from_date, to_date, aggtime='hour', aggloc='grid', interp_points=None, bounding_box=None):
        """
        Get the actual weather for each day from a given time window.
        Actual weather is actually a forecast made on given day - this is the best weather estimation
        we can get.

        Args:
            from_date (datetime.date): start of the timewindow
            to_date (datetime.date): end of the timewindow
            aggtime (str): time aggregation level; can be 'hour', 'day' or 'week'
            aggloc (str): location aggregation level; can be country', 'points', 'grid' or 'bbox'
            interp_points (list of dicts): list of interpolation points with each point represented
                as dict with fields 'lon' and 'lat' representing longtitude and lattitude if aggloc='points'
            bounding_box ([[lat1,lon1], [lat2,lon2]]): corner points of the bounding box if aggloc='bounding_box',
                order of the points is not important

        Returns:
            pandas.DataFrame: resulting object with weather measurements
        """
        assert type(from_date) == datetime.date
        assert type(to_date) == datetime.date
        assert from_date <= to_date
        assert aggtime in ['hour', 'day', 'week']
        assert aggloc in ['country', 'points', 'grid', 'bbox']

        if aggloc == 'points':
            if interp_points is None:
                raise ValueError(
                    "interp_points cannot be None if aggloc is set to 'points'.")
            interp_points = self._latslons_from_dict(interp_points)
        if aggloc == 'bounding_box':
            if bounding_box is None:
                raise ValueError(
                    "bounding_box cannot be None if aggloc is set to 'bounding_box'.")

        req_period = self.grib_msgs.loc[from_date:to_date]
        tmp_result = req_period[req_period['validDateTime'].dt.date ==
                                req_period['validityDateTime'].dt.date]

        # drop 'type' column
        tmp_result.drop('type', axis=1, inplace=True)

        # reset original index
        tmp_result.reset_index(drop=True, inplace=True)

        # point aggregation
        aggtype = 'mean' if aggloc == 'bbox' else 'one'
        tmp_result = self._aggregate_points(
            tmp_result, aggloc, aggtype=aggtype, interp_points=interp_points, bounding_box=bounding_box)

        # time aggregation
        tmp_result = self._aggregate_values(tmp_result, aggtime)

        return tmp_result

    def get_forecast(self, base_date, from_date, to_date, aggtime='hour', aggloc='grid', interp_points=None,
        bounding_box=None):
        """
        Get the weather forecast for a given time window from a given date.

        Args:
            base_date (datetime.date): base date for the forecast
            from_date (datetime.date): start of the time window
            end_date (datetime.date): end of the timewindow
            aggtime (str): time aggregation level; can be 'hour', 'day' or 'week'
            aggloc (str): location aggregation level; can be 'country', 'points', 'grid' or 'bbox'
            interp_points (list of dicts): list of interpolation points with each point represented
                as dict with fields 'lon' and 'lat' representing longtitude and lattitude if aggloc='points'
            bounding_box ([[lat1,lon1], [lat2,lon2]]): corner points of the bounding box if aggloc='bbox',
                order of the points is not important

        Returns:
            pandas.DataFrame: resulting object with weather measurements
        """
        assert type(base_date) == datetime.date
        assert type(from_date) == datetime.date
        assert type(to_date) == datetime.date
        assert base_date <= from_date <= to_date
        assert aggtime in ['hour', 'day', 'week']
        assert aggloc in ['country', 'points', 'grid', 'bbox']

        if aggloc == 'points':
            if interp_points is None:
                raise ValueError(
                    "interp_points cannot be None if aggloc is set to 'points'.")
            interp_points = self._latslons_from_dict(interp_points)
        if aggloc == 'bounding_box':
            if bounding_box is None:
                raise ValueError(
                    "bounding_box cannot be None if aggloc is set to 'bounding_box'.")

        req_period = self.grib_msgs.loc[base_date]

        # start with default (hourly) aggregation
        tmp_result = req_period[req_period['validityDateTime'].dt.date >= from_date]
        tmp_result = tmp_result[tmp_result['validityDateTime'].dt.date <= to_date]

        # drop 'type' column
        tmp_result.drop('type', axis=1, inplace=True)

        # reset original index
        tmp_result.reset_index(drop=True, inplace=True)

        # point aggregation
        aggtype = 'mean' if aggloc == 'bbox' else 'one'
        tmp_result = self._aggregate_points(
            tmp_result, aggloc, aggtype=aggtype, interp_points=interp_points, bounding_box=bounding_box)

        # time aggregation
        tmp_result = self._aggregate_values(tmp_result, aggtime)

        return tmp_result

    def export_qminer(self, filename, interp_points):
        """
        Export weather features for each date from dates to .tsv file.

        Args:
            filename (str): name of target file
            interp_points (list of dicts): list of interpolation points with each point represented
                as dict with fields 'lon' and 'lat' representing longtitude and lattitude
        Returns:
            pandas.DataFrame: resulting object with weather measurements
        """
        # export all dates
        dates = np.unique(sorted([dt.date() for dt in self.grib_msgs.validDateTime]))
        # get interpolation points
        lats, lons = self.grib_msgs.iloc[0]['lats'], self.grib_msgs.iloc[0]['lons']
        target_lats, target_lons = self._latslons_from_dict(interp_points)
        # only keep the values from closest point to each target
        closest = self._calc_closest(target_lats, target_lons, lats, lons)
        # weather features frame
        tf = self.grib_msgs
        # index on the predicted date
        tf = tf.set_index('validityDateTime', drop=False)
        tf = tf.sort_index()
        # WARNING: there is something wrong with ptype parameter
        tf = tf[tf.shortName != 'ptype']
        # interpolate all values
        tf['values'] = tf['values'].apply(lambda x: x[closest])

        # generate new dataframe
        rf = pd.DataFrame()
        rf['param'] = tf['shortName']
        rf['timestamp'] = tf['validityDateTime']
        rf['dayOffset'] = (tf['validityDateTime'] - tf['validDateTime']).apply(lambda x: x.days)

        # generate region values
        for i in range(len(interp_points)):
            rf[str(i)] = tf['values'].apply(lambda x: x[i])
        # transform region values from columns to rows
        rf = pd.melt(rf, id_vars=['param', 'timestamp', 'dayOffset'], var_name='region', value_name='value')
        rf['region'] = pd.to_numeric(rf['region'])

        rf.sort_values(by=['timestamp', 'region'], inplace=True)
        rf.to_csv(filename, sep='\t', index=False)

    def export_db(self, filename):
        """
        Export weather features to tsv file in MariaDB format.

        Args:
            filename (str): name of target file
        Returns:
            pandas.DataFrame: resulting object with weather measurements
        """
        # weather features frame
        df = self.grib_msgs

        def f(group):
            item = group.iloc[0]
            n = len(item.lats)

            offset = math.trunc((item.validityDateTime - item.validDateTime).total_seconds() / 3600.0)
            new_columns = {
                'date': [item.validDateTime] * n,
                'offset': [offset] * n,
                'latitude': list(item.lats),
                'longitude': list(item.lons)
            }

            for param_name, param_group in group.groupby('shortName'):
                new_columns[param_name] = param_group.iloc[0]['values']

            return pd.DataFrame(new_columns)

        df = df.groupby(['validDateTime', 'validityDateTime'], as_index=False).apply(f)
        df.sort_values(by=['date', 'offset'], inplace=True)
        df.to_csv(filename, sep='\t', index=False)

    def export(self, filename, interp_points, weather_params='all', forecast_offsets='all', regions='all'):
        """
        Export weather features for each date from dates to .tsv file.

        Args:
            filename (str): name of target file
            dates (list): list of dates (datetime.date)
            interp_points (list of dicts): list of interpolation points with each point represented
                as dict with fields 'lon' and 'lat' representing longtitude and lattitude

        Returns:
            pandas.DataFrame: resulting object with weather measurements
        """
        # export all dates
        dates = np.unique(sorted([dt.date() for dt in self.grib_msgs.validDateTime]))
        # get interpolation points
        lats, lons = self.grib_msgs.iloc[0]['lats'], self.grib_msgs.iloc[0]['lons']
        target_lats, target_lons = self._latslons_from_dict(interp_points)
        # only keep the values from closest point to each target
        closest = self._calc_closest(target_lats, target_lons, lats, lons)
        # weather features frame
        tf = self.grib_msgs
        # index on the predicted date
        tf = tf.set_index('validityDateTime', drop=False)
        tf = tf.sort_index()
        # feature collection
        feat_rows = list()
        # used weather parameters
        if weather_params == 'all': weather_params = np.unique(tf['shortName'])
        # used weather regions
        if regions == 'all': regions = list(range(len(interp_points)))
        # used forecast base_date offsets
        if forecast_offsets == 'all': forecast_offsets = list(range(-11, 1))
        # interpolate all values
        tf['values'] = tf['values'].apply(lambda x: x[closest])
        for curr_date_pos, curr_date in enumerate(dates):
            # process current date
            start_day = datetime.datetime.combine(curr_date, datetime.time(0,0))
            end_day = datetime.datetime.combine(curr_date, datetime.time(23,59))
            # forecast data regarding current date
            cf = tf.loc[start_day:end_day]
            # group by all unique dates on which forecast was made and weather parameters
            date_params_groups = cf.groupby(['validDateTime', 'shortName'])
            for group_name in date_params_groups.groups:
                pdf = date_params_groups.get_group(group_name)
                # are we interested in the forecast from day_offset days before?
                base_date = pdf['validDateTime'].iloc[0].date()
                day_offset = (base_date - curr_date).days
                if day_offset not in forecast_offsets: continue
                # forecasted params from base_date for date
                param_name = pdf.iloc[0]['shortName']
                # WARNING: there is something wrong with ptype
                if param_name == 'ptype':
                    continue
                # are we interested in this parameter?
                if param_name not in weather_params: continue
                # feature prefix
                feat_prefix = 'WEATHERFC%s%03d%s' % ('+' if day_offset >= 0 else '-', abs(day_offset), param_name)
                # describe accumulated parameter
                if param_name in ['sund', 'tp', 'sf']: # sun duration, total percitipation, snow fall
                    for from_hour, to_hour in [(0, 6), (6, 12), (12, 18), (6, 18)]:
                        cum_from = pdf.loc[datetime.time(from_hour):datetime.time(from_hour)]
                        if len(cum_from) == 0:
                            print("base_date: ", base_date, " curr_date: ", curr_date, " param_name: ", param_name, " at: ", from_hour, " missing!")
                            continue
                        else:
                            cum_from = cum_from.iloc[0]['values']

                        cum_to = pdf.loc[datetime.time(to_hour):datetime.time(to_hour)]
                        if len(cum_to) == 0:
                            print("base_date: ", base_date, " curr_date: ", curr_date, " param_name: ", param_name, " at: ", from_hour, " missing!")
                            continue
                        else:
                            cum_to = cum_to.iloc[0]['values']

                        for reg in regions:
                            feat_rows.append({
                                'validDate': curr_date,
                                'dayOffset': day_offset,
                                'region': reg,
                                'shortName': param_name,
                                'fromHour': from_hour,
                                'toHour': to_hour,
                                'value': cum_to[reg] - cum_from[reg],
                                'featureName': '%s%03dCUM%02d-%02d' % (feat_prefix, reg, from_hour, to_hour),
                                'aggFunc': 'cum'
                            })
                # describe instant parameter
                elif param_name in ['2t', 'ws', 'rh', 'sd', 'tcc'] : # temperature, wind-speed, relative humidity, snow depth
                    for func_name, func in zip(['min', 'mean', 'max'], [np.min, np.mean, np.max]):
                        for from_hour, to_hour in [(0, 6), (6, 12), (12, 18), (6, 18)]:
                            range_values = pdf.loc[datetime.time(from_hour, 0):datetime.time(to_hour, 0)]
                            for reg in regions:
                                feat_rows.append({
                                    'validDate': curr_date,
                                    'dayOffset': day_offset,
                                    'region': reg,
                                    'shortName': param_name,
                                    'fromHour': from_hour,
                                    'toHour': to_hour,
                                    'value': func(range_values['values'].apply(lambda x: x[reg])),
                                    'featureName': '%s%03d%s%02d-%02d' % (feat_prefix, reg, func_name.upper(), from_hour, to_hour),
                                    'aggFunc': func_name
                                })

        feat_df = pd.DataFrame.from_dict(feat_rows)
        feat_df.to_csv(filename, sep='\t', index=False)

class WeatherApi:
    """
    Interface for downloading weather data from MARS.

    Example:
        $ wa = WeatherApi()
    """

    def __init__(self, source, key=None, email=None):
        """
        Args:
            source (str): 'owm' for OpenWeatherMaps or 'ecmwf' 
        """
        assert source in ['ecmwf', 'owm']
        
        self.source = source
        if source == 'ecmwf':
            from .request import EcmwfServer
            self.server = EcmwfServer(key=key, email=email)
        elif source == 'owm':
            from .request import OwmServer
            if key is None:
                raise ValueError('API key for OpenWeatherMaps has to be specified via "api_key" argument')
            self.server = OwmServer(api_key=key)

    def get(self, target, from_date=None, to_date=None, base_time='midnight', steps=None, area=None, grid=(0.25, 0.25),
        city_name=None, city_id=None, latlon=None):
        """
        Execute a request with given parameters and store the result to 'target' file.

        Args:
            from_date (datetime.date): first base date of the forecast, default value is TODAY 
            to_date (datetime.date): last base date of the forecast, default value equals 'from_date'
            base_time (str): 'midnight' or 'noon'
            area: grid area for ECMWF query
            city_id: id of the city for OpenWeatherMaps query (https://openweathermap.org/forecast5)
            lonlat: longitude and latitude for OpenWeatherMaps query
        """
        assert isinstance(from_date, datetime.date) or from_date is None
        if from_date is None: 
            from_date = datetime.date.today()
        assert isinstance(to_date, datetime.date) or to_date is None
        if to_date is not None:
            assert from_date <= to_date
        assert base_time in ['midnight', 'noon']
        
        if self.source == 'ecmwf':
            from .request import WeatherReq

            # create new request
            req = WeatherReq()

            # set date
            req.set_date(from_date, end_date=to_date)

            # set target grib file
            req.set_target(target)

            # set base time
            if base_time == 'midnight':
                req.set_midnight()
            else:
                req.set_noon()

            if steps is None:
                # assume base time is 'midnight'
                # base_date is the date the forecast was made
                steps = []

                # current day + next three days
                for day_off in range(4):
                    steps += [day_off * 24 +
                                hour_off for hour_off in [0, 3, 6, 9, 12, 15, 18, 21]]

                # other 4 days
                for day_off in range(4, 8):
                    steps += [day_off * 24 +
                                hour_off for hour_off in [0, 6, 12, 18]]

                if base_time == 'noon':
                    steps = [step for step in steps in step - 12 >= 0]

            req.set_step(steps)

            if area is None:
                raise ValueError('No area is specified for ECMWF query.')
            req.set_area(area)
        
            # set grid resolution
            req.set_grid(grid)

            self.server.retrieve(req)

        elif self.source == 'owm':
            params = {}
            if sum(x is not None for x in [city_name, city_id, latlon]) != 1:
                raise ValueError('Exactly one of the fields city_name, city_id and lonlat needs to be specified.')
            
            if city_name is not None:
                params['q'] = city_name
            elif city_id is not None:
                params['id'] = city_id
            elif latlon is not None:
                params['lat'] = latlon[0]
                params['lon'] = latlon[1]
            
            self.server.retrieve(params, target)
        else:
            raise ValueError('Invalid weather source: %s' % self.source)