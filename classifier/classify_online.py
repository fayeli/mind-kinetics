#!/usr/bin/env python2

import random
import time
import threading
import csv
import numpy as np
from multiprocessing import Process
import sys
import serial

import socket
import json
from mdp import FlowException

from scipy import signal
import scipy.stats

from open_bci import *
# from open_bci_v3 import *

import classifier

from datetime import datetime

def generate_trials(N):
    L = [("left", -1), ("right", 1), ('baseline', 0)]

    d = list()

    for i in range(N):
        LL = list(L)
        random.shuffle(LL)
        d.extend(LL)

    return d

def initialize_board(port, baud):
    board = OpenBCIBoard(port, baud)
    # for i in range(100):
    #     print(board.ser.read())
    board.disconnect()

    board = OpenBCIBoard(port, baud)
    return board

def find_port():
    import platform, glob

    s = platform.system()

    p = glob.glob('/dev/ttyACM*')
    if len(p) >= 1:
        return p[0]
    else:
        return None

class MIOnline():

    def __init__(self, port=None, baud=115200):
        # self.board = initialize_board(port, baud)
        # port = find_port()
        #port = '/dev/tty.usbmodem1411'
        #port = '/dev/ttyACM1'
        port = '/dev/ttyACM0'

        self.board = OpenBCIBoard(port, baud)
        self.bg_thread = None
        self.bg_classify = None

        self.data = np.array([0.0]*8)
        self.y = np.array([0])
        self.trial = np.array([2])

        self.should_classify = False
        self.classify_loop = True
        self.out_sig = np.array([0])
        # self.controls = np.array([[0]*4])

        self.sock_send = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.ip = '127.0.0.1'
        self.port_send = 33333

        self.sock_receive = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.port_receive = 10000
        self.sock_receive.bind((self.ip, self.port_receive))

        self.threshold = 0.1

        # 0 for baseline, -1 for left hand, 1 for right hand
        # 2 for pause
        self.current_class = 0

        self.current_trial = 0
        self.trials = generate_trials(6)

        self.pause_now = True

        self.flow = None

        self.trial_interval = 4
        self.pause_interval = 2.5
        self.pre_trial = 2

        # self.trial_interval = 0.5
        # self.pause_interval = 0.5
 
        self.good_times = 0
        self.total_times = 0

        self.curr_event = None

        #self.arm_port = '/dev/ttyACM1'
        self.arm_port = None # for debugging without arm
        # self.arm_port = '/dev/tty.usbmodem1451'
        if self.arm_port:
            print('found arm on port {0}'.format(self.arm_port))
            self.arm = serial.Serial(self.arm_port, 115200);
        else:
            print('did not find arm')
            self.arm = None

        self.running_arm = False

    def stop(self):
        # resolve files and stuff
        self.board.should_stream = False
        self.should_classify = False
        self.classify_loop = False
        #self.bg_thread.join()
        self.bg_thread = None
        self.bg_classify = None
        self.data = np.array([0]*8)

    def disconnect(self):
        self.board.disconnect()

    def send_it(self, event, val=0, dir=None, accuracy=None):
        d = {
            'threshold': self.threshold,
            'val': val,
            'event': event,
            'dir': dir,
            'accuracy': accuracy
        }
        self.sock_send.sendto(json.dumps(d), (self.ip, self.port_send))
        # print(val, dirr)

    def classify(self):
        X = self.data[-500:]
        if classifier.should_preprocess:
            X = classifier.preprocess(X)
            # print(X.shape)

        
        out = self.flow(X)
        s = out[-1]
        if abs(s) > self.threshold:
            s = np.sign(s)
        else:
            s = 0

        if time.time() > self.start_trial + 0.5 and (not self.pause_now) and (not self.running_arm):
        #if (not self.pause_now) and (not self.running_arm):
            if s == self.current_class:
                self.good_times += 1
            self.total_times += 1

        output = out[-1][0]

        if self.running_arm and self.arm:
            if s == 1:
                self.arm.write('a')
            elif s == -1:
                self.arm.write('A')
        # else: # bias
        #     if self.current_class != 2:
        #         output += self.current_class * 0.3
        #         output = np.clip(output, -1, 1)


        if not self.pause_now:
            self.send_it('state', output)
            print('classify', output)


    def background_classify(self):
        while self.classify_loop:
            if len(self.data) > 50 and (not self.pause_now) and self.flow:
                self.classify()
                #time.sleep(0.05)
            else:
                time.sleep(0.1)

    def train_classifier(self):

        trial = y = data = None
        test = False
        while not test:
            trial, y, data = self.trial, self.y, self.data
            test = trial.shape[0] == y.shape[0] and y.shape[0] == data.shape[0]

            
        # last 6 trials (3 trials per class)
        n_back = 12
        min_trial = max(0, self.current_trial - (n_back - 1))

        good = np.logical_and(y != 2, trial >= min_trial)
        sigs_train = data[good]
        y_train = y[good] #.astype('float32')

        if classifier.should_preprocess:
            classifier.train_pre_flow(sigs_train)
            sigs_train, y_train = classifier.preprocess(sigs_train, y_train)
            # print(sigs_train.shape, y_train.shape)
            # print(list(y_train))

        y_train = y_train.astype('float32')
        
        # print(self.data.shape, self.y.shape, self.trial.shape)

        # inp = classifier.get_inp_xy(sigs_train, y_train)
        f = self.flow
        try:
            print('training classifier...')
            self.flow = classifier.get_flow(sigs_train, y_train)
            self.should_classify = True
            print('updated classifier!')
        except FlowException as e:
            self.flow = f
            print "FlowException error:\n{0}".format(e)


    def receive_sample(self, sample):
        try:
            t = time.time()
            sample = sample.channels
            # sample = sample.channel_data
            # print(sample)
            if not np.any(np.isnan(sample)):
                trial = np.append(self.trial, self.current_trial)
                y = np.append(self.y, self.current_class)
                data = np.vstack( (self.data, sample) )

                self.trial, self.y, self.data = trial, y, data
        except:
            pass

    def check_wait(self, wait_time):
        t0 = time.time()
        t = t0
        while t - t0 < wait_time:
            if self.curr_event != 'start':
                return True
            time.sleep(0.05)
            t = time.time()
        return False

    def save_data(self):
        date = datetime.now().strftime('%Y-%m-%d--%H-%M')
        fname = 'data/' + date
        np.savez_compressed(fname, data=self.data, y=self.y, trial=self.trial)
    
    def run_trials(self):

        self.pause_now = True
        self.send_it('pause', dir=self.trials[0][0])
        self.current_class = 2

        # reset data
        self.data = np.array([0.0]*8)
        self.y = np.array([0])
        self.trial = np.array([2])


        self.good_times = 0
        self.total_times = 0

        if self.check_wait(self.pause_interval):
            return


        for i in range(len(self.trials)):
            x, t = self.trials[i]

            self.current_trial = i

            print('{0} - {1}\t({2})'.format(i, x, self.data.shape))

            if self.flow:
                self.send_it('state', dir=x, val=0) # will classify
            else:
                self.send_it('state', dir=x, val=t)

            self.pause_now = False

            # if self.check_wait(self.pre_trial):
            #     break

            self.current_class = t

            self.start_trial = time.time()

            # time.sleep(self.trial_interval)
            if self.check_wait(self.trial_interval):
                break

            accuracy = None

            if self.total_times > 0:
                accuracy = float(self.good_times) / self.total_times
                print(accuracy, self.good_times, self.total_times)

            self.pause_now = True
            self.current_class = 2

            if i == len(self.trials) - 1:
                self.send_it('done', accuracy=accuracy)
                break


            if (i+1) % 6 == 0:
                self.send_it('classifying', dir=self.trials[i+1][0],
                             accuracy=accuracy)
                self.train_classifier()
                self.good_times = 0
                self.total_times = 0

                if self.check_wait(self.pause_interval):
                    break

            self.send_it('pause', dir=self.trials[i+1][0])

                # time.sleep(self.pause_interval)
            if self.check_wait(self.pause_interval):
                break

        self.save_data()
        
        self.pause_now = True

    def play_trials(self):
        self.pause_now = False
        self.running_arm = True
        while self.curr_event == 'play':
            time.sleep(0.1)
        self.running_arm = False
        self.pause_now = True

    def update_commands(self):
        print('updating commands...')
        while True:
            data = self.sock_receive.recv(4096)
            print(data)
            data = json.loads(data)
            event = data.get('event', None)
            self.curr_event = event

    def signal_check(self):
        while self.curr_event == 'setup':
            sig = self.data[-150:]
            b, a = signal.butter(3, (55.0/125, 65.0/125), 'bandstop')
            sig = signal.lfilter(b, a, sig, axis=0)

            b, a = signal.butter(3, (115.0/125, 125.0/125), 'bandstop')
            sig = signal.lfilter(b, a, sig, axis=0)

            if len(sig.shape) < 2:
                continue
                
            # print(sig.shape)
            for i in range(sig.shape[1]):
                sig[:, i] = signal.medfilt(sig[:, i], 3)

            freq, fourier = signal.welch(sig, 250.0, axis=0)

            z = np.any(abs(fourier) == 0, axis=0)
            out = ['0' for i in range(8)]

            # print(z)
            
            for i in range(8):
                if z[i]:
                    out[i] = 0
                else:
                    res = scipy.stats.linregress(freq, np.log(abs(fourier[:, i])))
                    slope, intercept, r_value, p_value, std_err = res
                    
                    # if i == 0:
                    print(i, intercept, slope)

                    
                    if slope < -0.025 and intercept < -19:
                    #if slope < -0.013 and slope > -0.025 and intercept < -41:
                        out[i] = 1
                    else:
                        out[i] = 0

            val = json.dumps(dict(zip(range(1,9), out)))
            self.send_it('setup', val=val)

            time.sleep(0.1)

            
            

    def manage_commands(self):
        while True:
            if self.curr_event == 'start':
                self.run_trials()
            elif self.curr_event == 'play':
                self.play_trials()
            elif self.curr_event == 'setup':
                self.signal_check()
            elif self.curr_event == 'end':
                self.data = np.array([0.0]*8)
                self.y = np.array([0])
                self.trial = np.array([2])
            elif self.curr_event == 'reset':
                self.flow = None
                self.data = np.array([0.0]*8)
                self.y = np.array([0])
                self.trial = np.array([2])
                self.curr_event = 'end'
                self.good_times = 0
                self.total_times = 0


            time.sleep(0.2)

    def start(self):

        if self.bg_thread:
            self.stop()


        #create a new thread in which the OpenBCIBoard object will stream data
        self.bg_thread = threading.Thread(
            # target=self.board.startStreaming,
            target = self.board.start,
            args=(self.receive_sample, ))
        self.bg_thread.start()

        self.classify_loop = True

        #create a new thread in which the OpenBCIBoard object will stream data
        self.bg_classify = threading.Thread(target=self.background_classify, args=())
        self.bg_classify.start()

        self.bg_commands = threading.Thread(target=self.update_commands, args=())
        self.bg_commands.start()

        self.manage_commands()


if __name__ == '__main__':
    online = MIOnline()
    online.start()
