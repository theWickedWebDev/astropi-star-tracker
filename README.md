# WORK IN PROGRESS
# Star Tracker // Astrophotography // Raspberry PI

Provides the ability to track any object in the sky, day or night. High precision allows for long exposures when capturing photographs.

## Stellarium integration

[![alt text](https://avatars.githubusercontent.com/u/7320160?s=70)](https://github.com/Stellarium/stellarium)

Control your EQ mount directly from Stellarium using the telescope plugin

## Worth mentioning
- Integrates with [Gphoto2/libgphoto2](https://github.com/gphoto/gphoto2)
- [Exiftool](https://github.com/exiftool/exiftool) integration automatically stores the weather, temperature, moon cycle, target object, RA/DEC, geolocation, etc... directly into the EXIF meta data on each image taken
- [Wttr](https://github.com/chubin/wttr.in) for weather information

<br/><br/>

# REST API

## Telescope

### - Calibrate by Object Name

Calibrate by _many_ object names in the sky. Stars, DSO's, etc...

`POST` | `/api/calibrate/by_name/?name=polaris`

### - Calibrate by Solar System Object

Calibrate by objects in our solar system. Sun, Jupiter, Mars, etc...

`POST` | `/api/calibrate/solar_system_object/?name=jupiter`

### - Calibrate by RA/DEC

Calibrate directly with coordinates

`POST` | `/api/calibrate/?ra=00h45m42.223s&dec=37d56m33.427s`

### - Goto Object Name

`POST` | `/api/goto/by_name/?name=polaris`

### - Goto by Solar System Object

`POST` | `/api/goto/solar_system_object/?name=jupiter`


### - Goto by RA/DEC

`POST` | `/api/goto/?ra=00h45m42.223s&dec=37d56m33.427s`

<br/>

## Camera (gphoto2)

### - Get settings

`GET` | `/api/camera/config/`


### - Update settings
```json
POST | /api/camera/config
{
  "iso": "800",
  "aperture": "3.5",
  "shutterspeed": "30"
}
```

### - Set Lens

```json
POST | 5/api/lens/55/ {}
```

### - Capture Light Frame
TODO

### - Capture Dark Frame
TODO

### - Capture Flat Frame
TODO

### - Capture Bias Frame
TODO

## Fully 3D-Printed, Equatorial Mount and extras

- mount
- filters
- manual => auto focuser knob stepper

Add info, links to files, etc...
<br/><br/>


# Web Application

- Live
- control camera, View photos, dew heater, peripherals
- control photography sessions, start/end time
- capture calibration frames
- plate solving
- astrometry.net integration
- histogram evaluation
- precalculated template photoshoots with suggested settings (perhaps show sample photo from last photoshoot)
- what's visible in my sky now - links to slew to them and capture photos
- calendar of events
- Calibration frame wizard, step checklist

<br/>
<br/>

# The Build

- Telescope Pier
- Housing for Raspberry Pi
- Dew Heater
- BOM
- Celestron Nexstar 6SE
- Canon T2I
- etc...