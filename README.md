# Withings2MQTT
Reads Withings scale API and sends to Home Assistant via MQTT

## Installing
Give public ip/address and port forward your port to your router.

Run Withings2MQTT.py --setup.

It opens in browser the Withings access request, accept it. 

If Withings can't access your callback, check it with your browser. 

You only need the port open when doing the initial setup. After that you can close it. 

## Running
When setup succeeded, it creates tokens.json and you can start getting values. 

It starts fetching all measurements from the start, and if you have old values, convert current date to unix time and save the value in state.json. 

If you have router where you can install similar scripts I'm using in Asus Merlin, the router can send http post to inform new measurement is available, and the polling value can be a lot longer. It still has to poll some time, so that token does not expire. 
