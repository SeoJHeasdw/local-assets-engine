import { app, BrowserWindow, dialog, ipcMain, session, shell } from "electron";
import { startStudio } from "./main/application.mjs";

startStudio({ app, BrowserWindow, dialog, ipcMain, session, shell });
