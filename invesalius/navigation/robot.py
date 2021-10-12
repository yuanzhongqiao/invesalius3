#--------------------------------------------------------------------------
# Software:     InVesalius - Software de Reconstrucao 3D de Imagens Medicas
# Copyright:    (C) 2001  Centro de Pesquisas Renato Archer
# Homepage:     http://www.softwarepublico.gov.br
# Contact:      invesalius@cti.gov.br
# License:      GNU - GPL 2 (LICENSE.txt/LICENCA.txt)
#--------------------------------------------------------------------------
#    Este programa e software livre; voce pode redistribui-lo e/ou
#    modifica-lo sob os termos da Licenca Publica Geral GNU, conforme
#    publicada pela Free Software Foundation; de acordo com a versao 2
#    da Licenca.
#
#    Este programa eh distribuido na expectativa de ser util, mas SEM
#    QUALQUER GARANTIA; sem mesmo a garantia implicita de
#    COMERCIALIZACAO ou de ADEQUACAO A QUALQUER PROPOSITO EM
#    PARTICULAR. Consulte a Licenca Publica Geral GNU para obter mais
#    detalhes.
#--------------------------------------------------------------------------
import numpy as np
import wx
import queue
import threading

import invesalius.data.bases as db
import invesalius.gui.dialogs as dlg
import invesalius.constants as const
from invesalius.pubsub import pub as Publisher

try:
    import invesalius.data.elfin as elfin
    import invesalius.data.elfin_processing as elfin_process
    has_robot = True
except ImportError:
    has_robot = False

class Robot():
    def __init__(self, tracker):
        self.tracker = tracker
        self.trk_init = None
        self.robot_target_queue = None
        self.object_at_target_queue = None
        self.process_tracker = None

        self.thread_robot = None

        self.robotcoordinates = RobotCoordinates()

        self.__bind_events()

    def __bind_events(self):
        Publisher.subscribe(self.OnSendCoordinates, 'Send coord to robot')
        Publisher.subscribe(self.OnUpdateRobotTargetMatrix, 'Robot target matrix')
        Publisher.subscribe(self.OnObjectTarget, 'Coil at target')

    def OnRobotConnection(self):
        if not self.tracker.trk_init[0][0] or not self.tracker.trk_init[1][0]:
            dlg.ShowNavigationTrackerWarning(self.tracker.tracker_id, self.tracker.trk_init[1])
            self.tracker.tracker_id = 0
            self.tracker.tracker_connected = False
        else:
            self.tracker.trk_init.append(self.robotcoordinates)
            self.process_tracker = elfin_process.TrackerProcessing()
            dlg_correg_robot = dlg.CreateTransformationMatrixRobot(self.tracker)
            if dlg_correg_robot.ShowModal() == wx.ID_OK:
                M_tracker_to_robot = dlg_correg_robot.GetValue()
                db.transform_tracker_to_robot.M_tracker_to_robot = M_tracker_to_robot
                self.robot_server = self.tracker.trk_init[1][0]
                self.trk_init = self.tracker.trk_init
            else:
                dlg.ShowNavigationTrackerWarning(self.tracker.tracker_id, 'disconnect')
                self.tracker.trk_init = None
                self.tracker.tracker_id = 0
                self.tracker.tracker_connected = False

    def StartRobotThreadNavigation(self, tracker, coord_queue):
        if tracker.event_robot.is_set():
            tracker.event_robot.clear()
        self.thread_robot = ControlRobot(self.trk_init, tracker, self.robotcoordinates,
                                         [coord_queue, self.robot_target_queue, self.object_at_target_queue],
                                         self.process_tracker, tracker.event_robot)
        self.thread_robot.start()

    def StopRobotThreadNavigation(self):
        self.thread_robot.join()
        #TODO: initialize process_tracker every time a "send coordinates to robot" is requested
        self.process_tracker.__init__()

    def OnSendCoordinates(self, coord):
        self.robot_server.SendCoordinates(coord)

    def OnUpdateRobotTargetMatrix(self, robot_tracker_flag, m_change_robot_to_head):
        try:
            self.robot_target_queue.put_nowait([robot_tracker_flag, m_change_robot_to_head])
        except queue.Full:
            pass

    def OnObjectTarget(self, state):
        try:
            if self.object_at_target_queue:
                self.object_at_target_queue.put_nowait(state)
        except queue.Full:
            pass

    def SetRobotQueues(self, queues):
        self.robot_target_queue, self.object_at_target_queue = queues


class RobotCoordinates():
    def __init__(self):
        self.coord = None

    def SetRobotCoordinates(self, coord):
        self.coord = coord

    def GetRobotCoordinates(self):
        return self.coord


class ControlRobot(threading.Thread):
    def __init__(self, trck_init, tracker, robotcoordinates, queues, process_tracker, event_robot):
        threading.Thread.__init__(self, name='ControlRobot')

        self.trck_init_robot = trck_init[1][0]
        self.trck_init_tracker = trck_init[0]
        self.trk_id = trck_init[2]
        self.tracker = tracker
        self.robotcoordinates = robotcoordinates
        self.robot_tracker_flag = False
        self.objattarget_flag = False
        self.target_flag = False
        self.m_change_robot_to_head = None
        self.coord_inv_old = None
        self.coord_queue = queues[0]
        self.robot_target_queue = queues[1]
        self.object_at_target_queue = queues[2]
        self.process_tracker = process_tracker
        self.event_robot = event_robot
        self.arc_motion_flag = False
        self.arc_motion_step_flag = None
        self.target_linear_out = None
        self.target_linear_in = None
        self.target_arc = None

    def get_coordinates_from_tracker_devices(self):
        coord_robot_raw = self.trck_init_robot.Run()
        coord_robot = np.array(coord_robot_raw)
        coord_robot[3], coord_robot[5] = coord_robot[5], coord_robot[3]
        self.robotcoordinates.SetRobotCoordinates(coord_robot)

        coord_raw, markers_flag = self.tracker.TrackerCoordinates.GetCoordinates()

        return coord_raw, coord_robot_raw, markers_flag

    def robot_move_decision(self, distance_target, coord_inv, coord_robot_raw, current_ref_filtered):
        if distance_target < const.ROBOT_ARC_THRESHOLD_DISTANCE and not self.arc_motion_flag:
            self.trck_init_robot.SendCoordinates(coord_inv, const.ROBOT_MOTIONS["normal"])

        elif distance_target >= const.ROBOT_ARC_THRESHOLD_DISTANCE or self.arc_motion_flag:
            actual_point = coord_robot_raw
            if not self.arc_motion_flag:
                coord_head = self.process_tracker.estimate_head_center(self.tracker,
                                                                       current_ref_filtered).tolist()

                self.target_linear_out, self.target_arc = self.process_tracker.compute_arc_motion(coord_robot_raw, coord_head,
                                                                                                  coord_inv)
                self.arc_motion_flag = True
                self.arc_motion_step_flag = const.ROBOT_MOTIONS["linear out"]

            if self.arc_motion_flag and self.arc_motion_step_flag == const.ROBOT_MOTIONS["linear out"]:
                coord = self.target_linear_out
                if np.allclose(np.array(actual_point), np.array(self.target_linear_out), 0, 1):
                    self.arc_motion_step_flag = const.ROBOT_MOTIONS["arc"]
                    coord = self.target_arc

            elif self.arc_motion_flag and self.arc_motion_step_flag == const.ROBOT_MOTIONS["arc"]:
                coord_head = self.process_tracker.estimate_head_center(self.tracker,
                                                                       current_ref_filtered).tolist()

                _, new_target_arc = self.process_tracker.compute_arc_motion(coord_robot_raw, coord_head,
                                                                            coord_inv)
                if np.allclose(np.array(new_target_arc[3:-1]), np.array(self.target_arc[3:-1]), 0, 1):
                    None
                else:
                    if self.process_tracker.correction_distance_calculation_target(coord_inv, coord_robot_raw) >= \
                            const.ROBOT_ARC_THRESHOLD_DISTANCE*0.8:
                        self.target_arc = new_target_arc

                coord = self.target_arc

                if np.allclose(np.array(actual_point), np.array(self.target_arc[3:-1]), 0, 10):
                    self.arc_motion_flag = False
                    self.arc_motion_step_flag = const.ROBOT_MOTIONS["normal"]
                    coord = coord_inv

            self.trck_init_robot.SendCoordinates(coord, self.arc_motion_step_flag)

    def robot_control(self, coords_tracker_in_robot, coord_robot_raw, markers_flag):
        coord_ref_tracker_in_robot = coords_tracker_in_robot[1]
        coord_obj_tracker_in_robot = coords_tracker_in_robot[2]

        if self.robot_tracker_flag:
            current_ref = coord_ref_tracker_in_robot
            if current_ref is not None and markers_flag[1]:
                current_ref_filtered = self.process_tracker.kalman_filter(current_ref)
                if self.process_tracker.compute_head_move_threshold(current_ref_filtered):
                    coord_inv = self.process_tracker.head_move_compensation(current_ref_filtered,
                                                                            self.m_change_robot_to_head)
                    if self.coord_inv_old is None:
                       self.coord_inv_old = coord_inv

                    if np.allclose(np.array(coord_inv), np.array(coord_robot_raw), 0, 0.01):
                        # print("At target within range 1")
                        pass
                    elif not np.allclose(np.array(coord_inv), np.array(self.coord_inv_old), 0, 5):
                        self.trck_init_robot.StopRobot()
                        self.coord_inv_old = coord_inv
                    else:
                        distance_target = self.process_tracker.correction_distance_calculation_target(coord_inv, coord_robot_raw)
                        self.robot_move_decision(distance_target, coord_inv, coord_robot_raw, current_ref_filtered)
                        self.coord_inv_old = coord_inv
            else:
                self.trck_init_robot.StopRobot()

    def run(self):
        while not self.event_robot.is_set():
            coords_tracker_in_robot, coord_robot_raw, markers_flag = self.get_coordinates_from_tracker_devices()

            if self.robot_target_queue.empty():
                None
            else:
                self.robot_tracker_flag, self.m_change_robot_to_head = self.robot_target_queue.get_nowait()
                self.robot_target_queue.task_done()

            if self.object_at_target_queue.empty():
                None
            else:
                self.target_flag = self.object_at_target_queue.get_nowait()
                self.object_at_target_queue.task_done()

            self.robot_control(coords_tracker_in_robot, coord_robot_raw, markers_flag)
