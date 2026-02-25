""" This contains EKF (Extended Kalman Filter) analysis plots """
import numpy as np
from bokeh.io import curdoc
from bokeh.models import Range1d
from bokeh.models.widgets import Div
from bokeh.layouts import column

from config import plot_width, plot_config, colors3, colors8, colors2
from helper import get_flight_mode_changes
from plotting import *
from plotted_tables import get_heading_html

#pylint: disable=cell-var-from-loop, undefined-loop-variable, too-many-statements


def _safe_get_dataset(ulog, topic_name):
    """Safely get a dataset, returning None if not available."""
    try:
        return ulog.get_dataset(topic_name)
    except (KeyError, IndexError):
        return None


def get_ekf_analysis_plots(ulog, px4_ulog, db_data, link_to_main_plots):
    """
    Get all bokeh plots shown on the EKF Analysis page.
    Provides detailed EKF2 innovation, variance, sensor bias, and health analysis.
    :return: list of bokeh plots
    """

    page_intro = """
<p>
This page provides detailed analysis of the EKF2 (Extended Kalman Filter) state estimator.
It shows innovation magnitudes vs their test limits, sensor biases, covariance health,
and filter consistency metrics. High innovation ratios or persistent biases indicate
estimation problems that may need parameter tuning.
</p>
<p>
<b>Key metrics to watch:</b>
<ul>
  <li><b>Innovation Test Ratios > 1.0:</b> The filter is rejecting measurements — sensor noise
      parameters may be too tight, or there's a real sensor issue.</li>
  <li><b>Velocity/Position Innovations:</b> Large sustained innovations indicate GPS or optical
      flow problems.</li>
  <li><b>Magnetometer Innovations:</b> Persistent mag innovations suggest magnetic interference
      or bad calibration.</li>
  <li><b>Accelerometer/Gyro Biases:</b> Large or drifting biases indicate sensor degradation
      or vibration issues.</li>
</ul>
</p>
    """

    curdoc().template_variables['title_html'] = get_heading_html(
        ulog, px4_ulog, db_data, None, [('Open Main Plots', link_to_main_plots)],
        'EKF Analysis') + page_intro

    plots = []
    data = ulog.data_list
    flight_mode_changes = get_flight_mode_changes(ulog)
    x_range_offset = (ulog.last_timestamp - ulog.start_timestamp) * 0.05
    x_range = Range1d(ulog.start_timestamp - x_range_offset,
                      ulog.last_timestamp + x_range_offset)

    # --- EKF2 Innovation Test Ratios ---
    # These are the key health indicators: ratio of innovation to test limit
    try:
        div = Div(text="<h4>EKF2 Innovation Test Ratios</h4>"
                  "<p>Values above 1.0 mean the filter is rejecting measurements. "
                  "Sustained values > 0.5 suggest the noise parameters need tuning.</p>")
        plots.append(column(div))

        # Velocity innovation test ratios
        estimator_status = _safe_get_dataset(ulog, 'estimator_status')
        if estimator_status is not None:
            es_data = estimator_status.data

            # Helper: only show a test ratio plot if the field has valid
            # (finite, non-NaN) data. Many test ratio fields exist in the
            # message but are all-NaN when the corresponding estimator is
            # not active (e.g. HAGL without a rangefinder, TAS without
            # an airspeed sensor).
            def _has_valid_ratio(field):
                return field in es_data and np.any(np.isfinite(es_data[field]))

            # Velocity innovations test ratio
            if _has_valid_ratio('vel_test_ratio'):
                data_plot = DataPlot(data, plot_config, 'estimator_status',
                                     y_axis_label='Ratio',
                                     title='Velocity Innovation Test Ratio',
                                     plot_height='small', x_range=x_range)
                data_plot.add_graph(['vel_test_ratio'], [colors8[0]],
                                    ['Velocity Test Ratio'])
                plot_flight_modes_background(data_plot, flight_mode_changes)
                if data_plot.finalize() is not None:
                    plots.append(data_plot.bokeh_plot)

            # Position innovation test ratio
            if _has_valid_ratio('pos_test_ratio'):
                data_plot = DataPlot(data, plot_config, 'estimator_status',
                                     y_axis_label='Ratio',
                                     title='Horizontal Position Innovation Test Ratio',
                                     plot_height='small', x_range=x_range)
                data_plot.add_graph(['pos_test_ratio'], [colors8[1]],
                                    ['Position Test Ratio'])
                plot_flight_modes_background(data_plot, flight_mode_changes)
                if data_plot.finalize() is not None:
                    plots.append(data_plot.bokeh_plot)

            # Height innovation test ratio
            if _has_valid_ratio('hgt_test_ratio'):
                data_plot = DataPlot(data, plot_config, 'estimator_status',
                                     y_axis_label='Ratio',
                                     title='Vertical Position Innovation Test Ratio',
                                     plot_height='small', x_range=x_range)
                data_plot.add_graph(['hgt_test_ratio'], [colors8[2]],
                                    ['Height Test Ratio'])
                plot_flight_modes_background(data_plot, flight_mode_changes)
                if data_plot.finalize() is not None:
                    plots.append(data_plot.bokeh_plot)

            # Magnetometer innovation test ratio
            if _has_valid_ratio('mag_test_ratio'):
                data_plot = DataPlot(data, plot_config, 'estimator_status',
                                     y_axis_label='Ratio',
                                     title='Magnetometer Innovation Test Ratio',
                                     plot_height='small', x_range=x_range)
                data_plot.add_graph(['mag_test_ratio'], [colors8[3]],
                                    ['Mag Test Ratio'])
                plot_flight_modes_background(data_plot, flight_mode_changes)
                if data_plot.finalize() is not None:
                    plots.append(data_plot.bokeh_plot)

            # Airspeed test ratio
            if _has_valid_ratio('tas_test_ratio'):
                data_plot = DataPlot(data, plot_config, 'estimator_status',
                                     y_axis_label='Ratio',
                                     title='Airspeed Innovation Test Ratio',
                                     plot_height='small', x_range=x_range)
                data_plot.add_graph(['tas_test_ratio'], [colors8[4]],
                                    ['Airspeed Test Ratio'])
                plot_flight_modes_background(data_plot, flight_mode_changes)
                if data_plot.finalize() is not None:
                    plots.append(data_plot.bokeh_plot)

            # Hagl test ratio (height above ground)
            if _has_valid_ratio('hagl_test_ratio'):
                data_plot = DataPlot(data, plot_config, 'estimator_status',
                                     y_axis_label='Ratio',
                                     title='Height Above Ground Innovation Test Ratio',
                                     plot_height='small', x_range=x_range)
                data_plot.add_graph(['hagl_test_ratio'], [colors8[5]],
                                    ['HAGL Test Ratio'])
                plot_flight_modes_background(data_plot, flight_mode_changes)
                if data_plot.finalize() is not None:
                    plots.append(data_plot.bokeh_plot)

    except (KeyError, IndexError) as error:
        print('Error in EKF test ratio plots: ' + str(error))


    # --- Velocity Innovations ---
    try:
        div = Div(text="<h4>Velocity Innovations</h4>"
                  "<p>Difference between predicted and measured velocity. "
                  "Large values indicate the filter prediction disagrees with GPS/flow.</p>")
        plots.append(column(div))

        innovations = _safe_get_dataset(ulog, 'estimator_innovations')
        if innovations is not None:
            inn_data = innovations.data

            # GPS velocity innovations
            data_plot = DataPlot(data, plot_config, 'estimator_innovations',
                                 y_axis_label='[m/s]',
                                 title='GPS Velocity Innovations (NED)',
                                 plot_height='small', x_range=x_range)
            fields = []
            labels = []
            field_colors = []
            for i, axis in enumerate(['N', 'E', 'D']):
                field_name = 'gps_vvel' if axis == 'D' else f'gps_hvel[{i}]'
                if field_name in inn_data:
                    fields.append(field_name)
                    labels.append(f'GPS Vel {axis}')
                    field_colors.append(colors3[i])
            if fields:
                data_plot.add_graph(fields, field_colors, labels)
                plot_flight_modes_background(data_plot, flight_mode_changes)
                if data_plot.finalize() is not None:
                    plots.append(data_plot.bokeh_plot)

            # GPS position innovations
            data_plot = DataPlot(data, plot_config, 'estimator_innovations',
                                 y_axis_label='[m]',
                                 title='GPS Position Innovations (NE)',
                                 plot_height='small', x_range=x_range)
            fields = []
            labels = []
            field_colors = []
            for i, axis in enumerate(['N', 'E']):
                field_name = f'gps_hpos[{i}]'
                if field_name in inn_data:
                    fields.append(field_name)
                    labels.append(f'GPS Pos {axis}')
                    field_colors.append(colors2[i])
            if fields:
                data_plot.add_graph(fields, field_colors, labels)
                plot_flight_modes_background(data_plot, flight_mode_changes)
                if data_plot.finalize() is not None:
                    plots.append(data_plot.bokeh_plot)

            # Vertical position innovation
            data_plot = DataPlot(data, plot_config, 'estimator_innovations',
                                 y_axis_label='[m]',
                                 title='Vertical Position Innovation (Baro/GPS)',
                                 plot_height='small', x_range=x_range)
            v_fields = []
            v_labels = []
            v_colors = []
            if 'baro_vpos' in inn_data:
                v_fields.append('baro_vpos')
                v_labels.append('Baro Vertical Pos')
                v_colors.append(colors8[0])
            if 'gps_vpos' in inn_data:
                v_fields.append('gps_vpos')
                v_labels.append('GPS Vertical Pos')
                v_colors.append(colors8[1])
            if 'rng_vpos' in inn_data:
                v_fields.append('rng_vpos')
                v_labels.append('Range Finder Vertical Pos')
                v_colors.append(colors8[2])
            if v_fields:
                data_plot.add_graph(v_fields, v_colors, v_labels)
                plot_flight_modes_background(data_plot, flight_mode_changes)
                if data_plot.finalize() is not None:
                    plots.append(data_plot.bokeh_plot)

            # Magnetometer innovations
            data_plot = DataPlot(data, plot_config, 'estimator_innovations',
                                 y_axis_label='[gauss]',
                                 title='Magnetometer Innovations (XYZ)',
                                 plot_height='small', x_range=x_range)
            mag_fields = []
            mag_labels = []
            mag_colors = []
            for i, axis in enumerate(['X', 'Y', 'Z']):
                field_name = f'mag_field[{i}]'
                if field_name in inn_data:
                    mag_fields.append(field_name)
                    mag_labels.append(f'Mag {axis}')
                    mag_colors.append(colors3[i])
            if mag_fields:
                data_plot.add_graph(mag_fields, mag_colors, mag_labels)
                plot_flight_modes_background(data_plot, flight_mode_changes)
                if data_plot.finalize() is not None:
                    plots.append(data_plot.bokeh_plot)

            # Heading innovation
            if 'heading' in inn_data:
                data_plot = DataPlot(data, plot_config, 'estimator_innovations',
                                     y_axis_label='[rad]',
                                     title='Heading Innovation',
                                     plot_height='small', x_range=x_range)
                data_plot.add_graph(['heading'], [colors8[6]], ['Heading'])
                plot_flight_modes_background(data_plot, flight_mode_changes)
                if data_plot.finalize() is not None:
                    plots.append(data_plot.bokeh_plot)

            # Optical flow innovations
            flow_fields = []
            flow_labels = []
            flow_colors = []
            for i, axis in enumerate(['X', 'Y']):
                field_name = f'flow[{i}]'
                if field_name in inn_data:
                    flow_fields.append(field_name)
                    flow_labels.append(f'Flow {axis}')
                    flow_colors.append(colors2[i])
            if flow_fields:
                data_plot = DataPlot(data, plot_config, 'estimator_innovations',
                                     y_axis_label='[rad/s]',
                                     title='Optical Flow Innovations',
                                     plot_height='small', x_range=x_range)
                data_plot.add_graph(flow_fields, flow_colors, flow_labels)
                plot_flight_modes_background(data_plot, flight_mode_changes)
                if data_plot.finalize() is not None:
                    plots.append(data_plot.bokeh_plot)

        else:
            # Fallback: try older estimator_innovations format or estimator_status
            div = Div(text="<p><i>estimator_innovations topic not found. "
                      "This log may use an older PX4 version.</i></p>")
            plots.append(column(div))

    except (KeyError, IndexError) as error:
        print('Error in EKF innovation plots: ' + str(error))


    # --- Innovation Variances (Test Limits) ---
    try:
        inn_var = _safe_get_dataset(ulog, 'estimator_innovation_variances')
        if inn_var is not None:
            div = Div(text="<h4>Innovation Variances (Test Limits)</h4>"
                      "<p>These represent the expected variance of each innovation. "
                      "Very small variances mean the filter trusts its prediction too much; "
                      "very large variances mean it trusts sensors too little.</p>")
            plots.append(column(div))
            iv_data = inn_var.data

            # GPS velocity innovation variances
            data_plot = DataPlot(data, plot_config, 'estimator_innovation_variances',
                                 y_axis_label='[(m/s)²]',
                                 title='GPS Velocity Innovation Variances',
                                 plot_height='small', x_range=x_range)
            vv_fields = []
            vv_labels = []
            vv_colors = []
            for i, axis in enumerate(['N', 'E']):
                field_name = f'gps_hvel[{i}]'
                if field_name in iv_data:
                    vv_fields.append(field_name)
                    vv_labels.append(f'GPS Vel Var {axis}')
                    vv_colors.append(colors3[i])
            if 'gps_vvel' in iv_data:
                vv_fields.append('gps_vvel')
                vv_labels.append('GPS Vel Var D')
                vv_colors.append(colors3[2])
            if vv_fields:
                data_plot.add_graph(vv_fields, vv_colors, vv_labels)
                plot_flight_modes_background(data_plot, flight_mode_changes)
                if data_plot.finalize() is not None:
                    plots.append(data_plot.bokeh_plot)

            # Position innovation variances
            data_plot = DataPlot(data, plot_config, 'estimator_innovation_variances',
                                 y_axis_label='[m²]',
                                 title='Position Innovation Variances',
                                 plot_height='small', x_range=x_range)
            pv_fields = []
            pv_labels = []
            pv_colors = []
            for i, axis in enumerate(['N', 'E']):
                field_name = f'gps_hpos[{i}]'
                if field_name in iv_data:
                    pv_fields.append(field_name)
                    pv_labels.append(f'GPS Pos Var {axis}')
                    pv_colors.append(colors2[i])
            if pv_fields:
                data_plot.add_graph(pv_fields, pv_colors, pv_labels)
                plot_flight_modes_background(data_plot, flight_mode_changes)
                if data_plot.finalize() is not None:
                    plots.append(data_plot.bokeh_plot)

    except (KeyError, IndexError) as error:
        print('Error in EKF innovation variance plots: ' + str(error))


    # --- Sensor Biases ---
    try:
        sensor_bias = _safe_get_dataset(ulog, 'estimator_sensor_bias')
        if sensor_bias is not None:
            div = Div(text="<h4>Estimated Sensor Biases</h4>"
                      "<p>The EKF estimates and compensates for sensor biases. "
                      "Large or drifting biases indicate sensor issues or vibration. "
                      "Gyro biases > 0.02 rad/s or accel biases > 0.3 m/s² are concerning.</p>")
            plots.append(column(div))
            sb_data = sensor_bias.data

            # Gyro bias
            data_plot = DataPlot(data, plot_config, 'estimator_sensor_bias',
                                 y_axis_label='[rad/s]',
                                 title='Gyroscope Bias Estimates',
                                 plot_height='small', x_range=x_range)
            gyro_fields = []
            gyro_labels = []
            gyro_colors = []
            for i, axis in enumerate(['X', 'Y', 'Z']):
                field_name = f'gyro_bias[{i}]'
                if field_name in sb_data:
                    gyro_fields.append(field_name)
                    gyro_labels.append(f'Gyro Bias {axis}')
                    gyro_colors.append(colors3[i])
            if gyro_fields:
                data_plot.add_graph(gyro_fields, gyro_colors, gyro_labels)
                plot_flight_modes_background(data_plot, flight_mode_changes)
                if data_plot.finalize() is not None:
                    plots.append(data_plot.bokeh_plot)

            # Accelerometer bias
            data_plot = DataPlot(data, plot_config, 'estimator_sensor_bias',
                                 y_axis_label='[m/s²]',
                                 title='Accelerometer Bias Estimates',
                                 plot_height='small', x_range=x_range)
            accel_fields = []
            accel_labels = []
            accel_colors = []
            for i, axis in enumerate(['X', 'Y', 'Z']):
                field_name = f'accel_bias[{i}]'
                if field_name in sb_data:
                    accel_fields.append(field_name)
                    accel_labels.append(f'Accel Bias {axis}')
                    accel_colors.append(colors3[i])
            if accel_fields:
                data_plot.add_graph(accel_fields, accel_colors, accel_labels)
                plot_flight_modes_background(data_plot, flight_mode_changes)
                if data_plot.finalize() is not None:
                    plots.append(data_plot.bokeh_plot)

            # Magnetometer bias
            data_plot = DataPlot(data, plot_config, 'estimator_sensor_bias',
                                 y_axis_label='[gauss]',
                                 title='Magnetometer Bias Estimates',
                                 plot_height='small', x_range=x_range)
            mag_bias_fields = []
            mag_bias_labels = []
            mag_bias_colors = []
            for i, axis in enumerate(['X', 'Y', 'Z']):
                field_name = f'mag_bias[{i}]'
                if field_name in sb_data:
                    mag_bias_fields.append(field_name)
                    mag_bias_labels.append(f'Mag Bias {axis}')
                    mag_bias_colors.append(colors3[i])
            if mag_bias_fields:
                data_plot.add_graph(mag_bias_fields, mag_bias_colors, mag_bias_labels)
                plot_flight_modes_background(data_plot, flight_mode_changes)
                if data_plot.finalize() is not None:
                    plots.append(data_plot.bokeh_plot)

    except (KeyError, IndexError) as error:
        print('Error in EKF sensor bias plots: ' + str(error))


    # --- Estimator States ---
    try:
        est_states = _safe_get_dataset(ulog, 'estimator_states')
        if est_states is not None:
            div = Div(text="<h4>Estimator State Variances</h4>"
                      "<p>Diagonal elements of the state covariance matrix. "
                      "Growing variances indicate the filter is losing confidence. "
                      "States: [0-3] Quaternion, [4-6] Velocity NED, [7-9] Position NED, "
                      "[10-12] Gyro Bias, [13-15] Accel Bias, [16-18] Mag Body, "
                      "[19-21] Wind Vel.</p>")
            plots.append(column(div))
            st_data = est_states.data

            # Quaternion state variances
            data_plot = DataPlot(data, plot_config, 'estimator_states',
                                 y_axis_label='Variance',
                                 title='Attitude State Variances (Quaternion)',
                                 plot_height='small', x_range=x_range)
            q_fields = []
            q_labels = []
            q_colors = []
            for i in range(4):
                field_name = f'covariances[{i}]'
                if field_name in st_data:
                    q_fields.append(field_name)
                    q_labels.append(f'Quat[{i}]')
                    q_colors.append(colors8[i])
            if q_fields:
                data_plot.add_graph(q_fields, q_colors, q_labels)
                plot_flight_modes_background(data_plot, flight_mode_changes)
                if data_plot.finalize() is not None:
                    plots.append(data_plot.bokeh_plot)

            # Velocity state variances
            data_plot = DataPlot(data, plot_config, 'estimator_states',
                                 y_axis_label='Variance [(m/s)²]',
                                 title='Velocity State Variances (NED)',
                                 plot_height='small', x_range=x_range)
            v_fields = []
            v_labels = []
            v_colors = []
            for i, axis in enumerate(['N', 'E', 'D']):
                field_name = f'covariances[{i+4}]'
                if field_name in st_data:
                    v_fields.append(field_name)
                    v_labels.append(f'Vel {axis}')
                    v_colors.append(colors3[i])
            if v_fields:
                data_plot.add_graph(v_fields, v_colors, v_labels)
                plot_flight_modes_background(data_plot, flight_mode_changes)
                if data_plot.finalize() is not None:
                    plots.append(data_plot.bokeh_plot)

            # Position state variances
            data_plot = DataPlot(data, plot_config, 'estimator_states',
                                 y_axis_label='Variance [m²]',
                                 title='Position State Variances (NED)',
                                 plot_height='small', x_range=x_range)
            p_fields = []
            p_labels = []
            p_colors = []
            for i, axis in enumerate(['N', 'E', 'D']):
                field_name = f'covariances[{i+7}]'
                if field_name in st_data:
                    p_fields.append(field_name)
                    p_labels.append(f'Pos {axis}')
                    p_colors.append(colors3[i])
            if p_fields:
                data_plot.add_graph(p_fields, p_colors, p_labels)
                plot_flight_modes_background(data_plot, flight_mode_changes)
                if data_plot.finalize() is not None:
                    plots.append(data_plot.bokeh_plot)

    except (KeyError, IndexError) as error:
        print('Error in EKF state plots: ' + str(error))


    # --- Estimator Status Flags ---
    try:
        estimator_status = _safe_get_dataset(ulog, 'estimator_status')
        if estimator_status is not None:
            div = Div(text="<h4>Estimator Health & Control Flags</h4>"
                      "<p>Filter control mode and health status. Non-zero health or "
                      "timeout flags indicate estimation problems.</p>")
            plots.append(column(div))
            es_data = estimator_status.data

            # Innovation check flags (detailed)
            # Try legacy innovation_check_flags bitmask first, then fall back
            # to individual reject_* fields in estimator_status_flags (newer PX4)
            if 'innovation_check_flags' in es_data:
                data_plot = DataPlot(data, plot_config, 'estimator_status',
                                     y_start=0, title='Innovation Check Flags (Detailed)',
                                     plot_height='normal', x_range=x_range)
                plot_data = []
                plot_labels = []
                input_data = [
                    ('Velocity Check', (es_data['innovation_check_flags']) & 0x1),
                    ('Horiz Position Check', (es_data['innovation_check_flags'] >> 1) & 1),
                    ('Vert Position Check', (es_data['innovation_check_flags'] >> 2) & 1),
                    ('Mag X Check', (es_data['innovation_check_flags'] >> 3) & 1),
                    ('Mag Y Check', (es_data['innovation_check_flags'] >> 4) & 1),
                    ('Mag Z Check', (es_data['innovation_check_flags'] >> 5) & 1),
                    ('Yaw Check', (es_data['innovation_check_flags'] >> 6) & 1),
                    ('Airspeed Check', (es_data['innovation_check_flags'] >> 7) & 1),
                    ('Sideslip Check', (es_data['innovation_check_flags'] >> 8) & 1),
                    ('Height to Ground Check', (es_data['innovation_check_flags'] >> 9) & 1),
                    ('Optical Flow X Check', (es_data['innovation_check_flags'] >> 10) & 1),
                    ('Optical Flow Y Check', (es_data['innovation_check_flags'] >> 11) & 1),
                ]
                for cur_label, cur_data in input_data:
                    if np.amax(cur_data) > 0.1:
                        data_label = 'flag_' + str(len(plot_data))
                        plot_data.append(lambda d, data=cur_data, label=data_label: (label, data))
                        plot_labels.append(cur_label)
                        if len(plot_data) >= 8:
                            break

                if len(plot_data) == 0:
                    plot_data = [lambda d: ('flags', input_data[0][1])]
                    plot_labels = ['All OK (Velocity Check shown)']
                data_plot.add_graph(plot_data, colors8[0:len(plot_data)], plot_labels)
                plot_flight_modes_background(data_plot, flight_mode_changes)
                if data_plot.finalize() is not None:
                    plots.append(data_plot.bokeh_plot)
            else:
                # Newer PX4: use estimator_status_flags with individual reject fields
                esf = _safe_get_dataset(ulog, 'estimator_status_flags')
                if esf is not None:
                    esf_data = esf.data
                    data_plot = DataPlot(data, plot_config, 'estimator_status_flags',
                                         y_start=0,
                                         title='Innovation Rejection Flags (Detailed)',
                                         plot_height='normal', x_range=x_range)
                    plot_data = []
                    plot_labels = []
                    reject_fields = [
                        ('reject_hor_vel', 'Horiz Velocity'),
                        ('reject_ver_vel', 'Vert Velocity'),
                        ('reject_hor_pos', 'Horiz Position'),
                        ('reject_ver_pos', 'Vert Position'),
                        ('reject_yaw', 'Yaw'),
                        ('reject_airspeed', 'Airspeed'),
                        ('reject_sideslip', 'Sideslip'),
                        ('reject_hagl', 'Height Above Ground'),
                        ('reject_optflow_x', 'Optical Flow X'),
                        ('reject_optflow_y', 'Optical Flow Y'),
                    ]
                    for field_name, cur_label in reject_fields:
                        if field_name in esf_data and np.amax(esf_data[field_name]) > 0.1:
                            cur_data = esf_data[field_name]
                            data_label = 'flag_' + str(len(plot_data))
                            plot_data.append(lambda d, data=cur_data, label=data_label: (label, data))
                            plot_labels.append(cur_label)
                            if len(plot_data) >= 8:
                                break

                    if len(plot_data) == 0:
                        # Show a flat zero line to indicate all OK
                        first_field = next((f for f, _ in reject_fields if f in esf_data), None)
                        if first_field is not None:
                            plot_data = [lambda d, f=first_field: ('flags', d[f])]
                            plot_labels = ['All OK (Horiz Velocity shown)']
                    if plot_data:
                        data_plot.add_graph(plot_data, colors8[0:len(plot_data)], plot_labels)
                        plot_flight_modes_background(data_plot, flight_mode_changes)
                        if data_plot.finalize() is not None:
                            plots.append(data_plot.bokeh_plot)

                    # Also show fault status flags (mag, acc, etc.)
                    fault_fields = [
                        ('fs_bad_mag_x', 'Bad Mag X'),
                        ('fs_bad_mag_y', 'Bad Mag Y'),
                        ('fs_bad_mag_z', 'Bad Mag Z'),
                        ('fs_bad_hdg', 'Bad Heading'),
                        ('fs_bad_airspeed', 'Bad Airspeed'),
                        ('fs_bad_acc_bias', 'Bad Accel Bias'),
                        ('fs_bad_acc_vertical', 'Bad Accel Vertical'),
                        ('fs_bad_acc_clipping', 'Bad Accel Clipping'),
                    ]
                    fault_plot_data = []
                    fault_plot_labels = []
                    for field_name, cur_label in fault_fields:
                        if field_name in esf_data and np.amax(esf_data[field_name]) > 0.1:
                            cur_data = esf_data[field_name]
                            data_label = 'fault_' + str(len(fault_plot_data))
                            fault_plot_data.append(
                                lambda d, data=cur_data, label=data_label: (label, data))
                            fault_plot_labels.append(cur_label)
                            if len(fault_plot_data) >= 8:
                                break
                    if fault_plot_data:
                        data_plot2 = DataPlot(data, plot_config, 'estimator_status_flags',
                                              y_start=0,
                                              title='EKF Fault Status Flags',
                                              plot_height='small', x_range=x_range)
                        data_plot2.add_graph(fault_plot_data,
                                             colors8[0:len(fault_plot_data)],
                                             fault_plot_labels)
                        plot_flight_modes_background(data_plot2, flight_mode_changes)
                        if data_plot2.finalize() is not None:
                            plots.append(data_plot2.bokeh_plot)

            # Health and timeout flags
            data_plot = DataPlot(data, plot_config, 'estimator_status',
                                 y_start=0, title='Health & Timeout Flags',
                                 plot_height='small', x_range=x_range)
            data_plot.add_graph(
                [lambda d: ('health', d['health_flags']),
                 lambda d: ('timeout', d['timeout_flags'])],
                [colors8[0], colors8[1]],
                ['Health Flags', 'Timeout Flags'])
            plot_flight_modes_background(data_plot, flight_mode_changes)
            if data_plot.finalize() is not None:
                plots.append(data_plot.bokeh_plot)

    except (KeyError, IndexError) as error:
        print('Error in EKF flags plots: ' + str(error))


    # --- GPS Quality Metrics ---
    try:
        gps = _safe_get_dataset(ulog, 'vehicle_gps_position')
        if gps is not None:
            div = Div(text="<h4>GPS Quality Metrics</h4>"
                      "<p>GPS accuracy and satellite count affect EKF performance.</p>")
            plots.append(column(div))
            gps_data = gps.data

            # Satellite count
            sat_field = None
            if 'satellites_used' in gps_data:
                sat_field = 'satellites_used'
            elif 'satellites_visible' in gps_data:
                sat_field = 'satellites_visible'

            if sat_field:
                data_plot = DataPlot(data, plot_config, 'vehicle_gps_position',
                                     y_axis_label='Count',
                                     title='GPS Satellites Used',
                                     plot_height='small', x_range=x_range)
                data_plot.add_graph([sat_field], [colors8[0]], ['Satellites'])
                plot_flight_modes_background(data_plot, flight_mode_changes)
                if data_plot.finalize() is not None:
                    plots.append(data_plot.bokeh_plot)

            # Position accuracy
            acc_fields = []
            acc_labels = []
            acc_colors = []
            if 'eph' in gps_data:
                acc_fields.append('eph')
                acc_labels.append('Horizontal Accuracy (EPH)')
                acc_colors.append(colors8[0])
            if 'epv' in gps_data:
                acc_fields.append('epv')
                acc_labels.append('Vertical Accuracy (EPV)')
                acc_colors.append(colors8[1])
            if acc_fields:
                data_plot = DataPlot(data, plot_config, 'vehicle_gps_position',
                                     y_axis_label='[m]',
                                     title='GPS Position Accuracy',
                                     plot_height='small', x_range=x_range)
                data_plot.add_graph(acc_fields, acc_colors, acc_labels)
                plot_flight_modes_background(data_plot, flight_mode_changes)
                if data_plot.finalize() is not None:
                    plots.append(data_plot.bokeh_plot)

            # Speed accuracy
            if 's_variance_m_s' in gps_data:
                data_plot = DataPlot(data, plot_config, 'vehicle_gps_position',
                                     y_axis_label='[m/s]',
                                     title='GPS Speed Accuracy',
                                     plot_height='small', x_range=x_range)
                data_plot.add_graph(['s_variance_m_s'], [colors8[2]],
                                    ['Speed Variance'])
                plot_flight_modes_background(data_plot, flight_mode_changes)
                if data_plot.finalize() is not None:
                    plots.append(data_plot.bokeh_plot)

    except (KeyError, IndexError) as error:
        print('Error in GPS quality plots: ' + str(error))


    # --- Vibration Metrics ---
    try:
        vibe_header_added = False

        # Try legacy vibe[0-2] in estimator_status first
        vibe = _safe_get_dataset(ulog, 'estimator_status')
        if vibe is not None and 'vibe[0]' in vibe.data:
            div = Div(text="<h4>Vibration Metrics (from EKF)</h4>"
                      "<p>High vibration degrades IMU measurements and EKF performance. "
                      "Values above 30 m/s² indicate serious vibration issues.</p>")
            plots.append(column(div))
            vibe_header_added = True

            data_plot = DataPlot(data, plot_config, 'estimator_status',
                                 y_axis_label='[m/s²]',
                                 title='IMU Vibration Levels',
                                 plot_height='small', x_range=x_range)
            vibe_fields = []
            vibe_labels = []
            vibe_colors = []
            for i, axis in enumerate(['X', 'Y', 'Z']):
                field_name = f'vibe[{i}]'
                if field_name in vibe.data:
                    vibe_fields.append(field_name)
                    vibe_labels.append(f'Vibe {axis}')
                    vibe_colors.append(colors3[i])
            if vibe_fields:
                data_plot.add_graph(vibe_fields, vibe_colors, vibe_labels)
                plot_flight_modes_background(data_plot, flight_mode_changes)
                if data_plot.finalize() is not None:
                    plots.append(data_plot.bokeh_plot)

        # Check vehicle_imu_status for vibration metrics and clipping
        imu_status = _safe_get_dataset(ulog, 'vehicle_imu_status')
        if imu_status is not None:
            imu_data = imu_status.data

            if not vibe_header_added:
                div = Div(text="<h4>Vibration Metrics (from IMU)</h4>"
                          "<p>High vibration degrades IMU measurements and EKF performance. "
                          "Accel vibration metric above 1.0 or clipping events indicate "
                          "serious vibration issues.</p>")
                plots.append(column(div))
                vibe_header_added = True

            # Vibration metrics (newer PX4 firmware)
            vmetric_fields = []
            vmetric_labels = []
            vmetric_colors = []
            if 'accel_vibration_metric' in imu_data:
                vmetric_fields.append('accel_vibration_metric')
                vmetric_labels.append('Accel Vibration')
                vmetric_colors.append(colors3[0])
            if 'gyro_vibration_metric' in imu_data:
                vmetric_fields.append('gyro_vibration_metric')
                vmetric_labels.append('Gyro Vibration')
                vmetric_colors.append(colors3[1])
            if 'delta_angle_coning_metric' in imu_data:
                vmetric_fields.append('delta_angle_coning_metric')
                vmetric_labels.append('Coning Metric')
                vmetric_colors.append(colors3[2])
            if vmetric_fields:
                data_plot = DataPlot(data, plot_config, 'vehicle_imu_status',
                                     y_axis_label='Metric',
                                     title='IMU Vibration Metrics',
                                     plot_height='small', x_range=x_range)
                data_plot.add_graph(vmetric_fields, vmetric_colors, vmetric_labels)
                plot_flight_modes_background(data_plot, flight_mode_changes)
                if data_plot.finalize() is not None:
                    plots.append(data_plot.bokeh_plot)

            # Clipping events
            clip_fields = []
            clip_labels = []
            clip_colors = []
            for i, axis in enumerate(['X', 'Y', 'Z']):
                field_name = f'accel_clipping[{i}]'
                if field_name in imu_data:
                    clip_fields.append(field_name)
                    clip_labels.append(f'Accel Clipping {axis}')
                    clip_colors.append(colors3[i])
            if clip_fields:
                data_plot = DataPlot(data, plot_config, 'vehicle_imu_status',
                                     y_axis_label='Count',
                                     title='IMU Accelerometer Clipping Events',
                                     plot_height='small', x_range=x_range)
                data_plot.add_graph(clip_fields, clip_colors, clip_labels)
                plot_flight_modes_background(data_plot, flight_mode_changes)
                if data_plot.finalize() is not None:
                    plots.append(data_plot.bokeh_plot)

    except (KeyError, IndexError) as error:
        print('Error in vibration plots: ' + str(error))


    # Summary section
    try:
        div_text = "<h4>EKF Analysis Summary</h4><ul>"
        estimator_status = _safe_get_dataset(ulog, 'estimator_status')
        if estimator_status is not None:
            es_data = estimator_status.data

            # Check innovation test ratios
            for ratio_name, display_name in [
                ('vel_test_ratio', 'Velocity'),
                ('pos_test_ratio', 'Position'),
                ('hgt_test_ratio', 'Height'),
                ('mag_test_ratio', 'Magnetometer'),
                ('tas_test_ratio', 'Airspeed'),
                ('hagl_test_ratio', 'Height Above Ground'),
            ]:
                if ratio_name in es_data and np.any(np.isfinite(es_data[ratio_name])):
                    max_ratio = np.nanmax(es_data[ratio_name])
                    mean_ratio = np.nanmean(es_data[ratio_name])
                    color = '#d55e00' if max_ratio > 1.0 else (
                        '#e69f00' if max_ratio > 0.5 else '#009e73')
                    status = 'FAIL' if max_ratio > 1.0 else (
                        'WARNING' if max_ratio > 0.5 else 'OK')
                    div_text += (f"<li><span style='color:{color}'><b>{status}</b></span> "
                                 f"{display_name}: max={max_ratio:.3f}, mean={mean_ratio:.3f}</li>")

            # Check innovation check flags (legacy) or reject fields (newer PX4)
            if 'innovation_check_flags' in es_data:
                total_flags = np.sum(es_data['innovation_check_flags'] > 0)
                pct_flags = 100.0 * total_flags / len(es_data['innovation_check_flags'])
                color = '#d55e00' if pct_flags > 10 else (
                    '#e69f00' if pct_flags > 1 else '#009e73')
                div_text += (f"<li><span style='color:{color}'><b>Innovation Rejections:</b></span> "
                             f"{pct_flags:.1f}% of samples had at least one flag set</li>")
            else:
                esf = _safe_get_dataset(ulog, 'estimator_status_flags')
                if esf is not None:
                    esf_data = esf.data
                    reject_fields = [f for f in esf_data.keys() if f.startswith('reject_')]
                    if reject_fields:
                        # Combine all reject flags into one metric
                        any_reject = np.zeros(len(esf_data['timestamp']), dtype=bool)
                        active_rejects = []
                        for field in reject_fields:
                            mask = esf_data[field] >= 1
                            if np.any(mask):
                                pct = 100.0 * np.sum(mask) / len(mask)
                                active_rejects.append(
                                    (field.replace('reject_', ''), pct))
                            any_reject |= mask
                        pct_any = 100.0 * np.sum(any_reject) / len(any_reject)
                        color = '#d55e00' if pct_any > 10 else (
                            '#e69f00' if pct_any > 1 else '#009e73')
                        div_text += (f"<li><span style='color:{color}'>"
                                     f"<b>Innovation Rejections:</b></span> "
                                     f"{pct_any:.1f}% of samples had at least "
                                     f"one rejection</li>")
                        for name, pct in sorted(active_rejects, key=lambda x: -x[1]):
                            div_text += (f"<li style='margin-left:20px'>"
                                         f"{name}: {pct:.1f}%</li>")

        div_text += "</ul>"
        div = Div(text=div_text, width=int(plot_width * 0.9))
        plots.append(column(div))

    except (KeyError, IndexError) as error:
        print('Error in EKF summary: ' + str(error))


    # Defensive: filter out any None values that might have crept in
    plots = [p for p in plots if p is not None]

    if len(plots) == 0:
        div = Div(text="<p>No EKF data found in this log. "
                  "Make sure estimator_status and related topics are logged.</p>")
        plots.append(column(div))

    return plots
