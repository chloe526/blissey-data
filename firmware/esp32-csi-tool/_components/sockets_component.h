#include <stdio.h>
#include <string.h>
#include <errno.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <unistd.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

// One UDP data frame per period. The AP drains port 2223 instead of replying.
void socket_transmitter_sta_loop(bool (*is_wifi_connected)()) {
    const char payload[] = "CSI_PROBE";
    struct sockaddr_in destination = {};
    destination.sin_family = AF_INET;
    destination.sin_port = htons(2223);
    inet_aton("192.168.4.1", &destination.sin_addr);
    const TickType_t period = pdMS_TO_TICKS(1000 / CONFIG_PACKET_RATE);
    configASSERT(period > 0);
    for (;;) {
        while (!is_wifi_connected()) vTaskDelay(pdMS_TO_TICKS(100));
        int fd = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
        if (fd < 0) { vTaskDelay(pdMS_TO_TICKS(1000)); continue; }
        struct timeval timeout = { .tv_sec = 0, .tv_usec = 100000 };
        setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof(timeout));
        printf("SENSING_TX: rate=%d Hz, destination=192.168.4.1:2223\n", CONFIG_PACKET_RATE);
        TickType_t next = xTaskGetTickCount();
        unsigned failures = 0;
        while (is_wifi_connected()) {
            int sent = sendto(fd, payload, sizeof(payload)-1, 0,
                              (struct sockaddr *)&destination, sizeof(destination));
            failures = sent == sizeof(payload)-1 ? 0 : failures + 1;
            if (failures >= 50) {
                printf("SENSING_TX: repeated send failure; reconnecting\n");
                esp_wifi_disconnect(); // STA disconnect event starts reconnection.
                break;
            }
            vTaskDelayUntil(&next, period);
            // After a long stall, resume at the next period instead of bursting.
            if (xTaskGetTickCount() - next > period) next = xTaskGetTickCount();
        }
        close(fd);
        vTaskDelay(pdMS_TO_TICKS(100));
    }
}
