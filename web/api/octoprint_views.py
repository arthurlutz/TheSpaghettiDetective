from datetime import datetime, timedelta, timezone
import time
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.parsers import MultiPartParser
from django.conf import settings
import requests
import json

from lib.file_storage import save_file_obj
from lib import redis
from app.models import *
from app.notifications import send_failure_alert
from lib.prediction import update_prediction_with_detections, is_failing

STATUS_TTL_SECONDS = 240
ALERT_COOLDOWN_SECONDS = 120

def alert_if_needed(printer):
    last_acknowledge = printer.alert_acknowledged_at or datetime.fromtimestamp(0, timezone.utc)
    if printer.current_print_alerted_at \
        or (datetime.now(timezone.utc) - last_acknowledge).total_seconds() < ALERT_COOLDOWN_SECONDS:
        return

    printer.set_alert()

    pause_print = printer.action_on_failure == Printer.PAUSE
    if pause_print:
        printer.pause_print_on_failure()

    send_failure_alert(printer, pause_print)

def command_response(printer):
    commands = PrinterCommand.objects.filter(printer=printer, status=PrinterCommand.PENDING)
    resp = Response({'commands': [ json.loads(c.command) for c in commands ]})
    commands.update(status=PrinterCommand.SENT)
    return resp

def ml_api_auth_headers():
    return {"Authorization": "Bearer {}".format(settings.ML_API_TOKEN)} if settings.ML_API_TOKEN else {}

class OctoPrintPicView(APIView):
    permission_classes = (IsAuthenticated,)
    parser_classes = (MultiPartParser,)

    def post(self, request):
        printer = request.auth

        pic = request.data['pic']
        internal_url, external_url = save_file_obj('raw/{}/{}.jpg'.format(printer.id, int(time.time())), pic, settings.PICS_CONTAINER)

        redis.printer_pic_set(printer.id, {'img_url': external_url}, ex=STATUS_TTL_SECONDS)

        if not printer.is_printing():
            return command_response(printer)

        req = requests.get(settings.ML_API_HOST + '/p', params={'img': internal_url}, headers=ml_api_auth_headers(), verify=False)
        req.raise_for_status()
        resp = req.json()

        prediction = PrinterPrediction.objects.get(printer=printer)
        update_prediction_with_detections(prediction, resp['detections'])
        prediction.save()

        if is_failing(prediction, printer.detective_sensitivity):
            alert_if_needed(printer)

        return command_response(printer)


class OctoPrintStatusView(APIView):
    permission_classes = (IsAuthenticated,)

    def post(self, request):

        def file_printing(octoprint_data):
            printing = False
            flags = octoprint_data.get('state', {}).get('flags', {})
            for flag in ('cancelling', 'paused', 'pausing', 'printing', 'resuming', 'finishing'):
                if flags.get(flag, False):
                    printing = True

            filename = octoprint_data.get('job', {}).get('file', {}).get('name')
            return filename, printing, octoprint_data.get('state', {}).get('text')

        printer = request.auth

        status = request.data
        octo_data = status.get('octoprint_data', {})
        filename, printing, text = file_printing(octo_data)
        seconds_left = octo_data.get('progress', {}).get('printTimeLeft') or -1

        redis.printer_status_set(printer.id, {'text': text, 'seconds_left': seconds_left}, ex=STATUS_TTL_SECONDS)
        if printing:
            printer.set_current_print(filename)
        else:
            printer.unset_current_print()

        return command_response(printer)
